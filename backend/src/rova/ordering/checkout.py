"""A8.1 checkout (reduced): one vendor per line chosen by the fee-blind
ranking engine, grouped into one order per vendor, SM-01 SUBMIT_CART applied,
order_fee_schedule captured (A11.2), and the credit gate (A10) enforced for
CREDIT_N_DAYS sub-baskets. Basket-level MOV blocking, RFQ fallback and the
DROP/MOVE_LINES vendor decisions (A6.2) are not wired in this session
(manifest) — a line with no fresh candidate raises GUARD_FAILED instead of
becoming an RFQ line."""
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.core.clock import now
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.money import money_str
from rova.credit.gate import check as credit_check
from rova.domain import timers
from rova.domain.fsm import Ctx
from rova.domain.machines.registry import MACHINES
from rova.domain.machines.sm20_eta_estimate import create_provisional
from rova.fees.selection import active_schedules_for
from rova.ranking.engine import rank
from rova.ranking.inputs import build_ranking_input


def _create_order_for_vendor(
    session: Session, *, req: dict, pharmacy: dict, vendor_id: str, vendor_lines: list[dict],
    chosen_offer: dict, payment_overrides: dict[str, str],
) -> dict:
    """The per-vendor order-creation body, extracted so `allocate_remainder`
    (defect 4 fix, below) can create the same shape of single-vendor order
    for a rerouted remainder line without going through `checkout()` itself
    — `checkout()` only enters from `DRAFT` or `CONFIRMED` (R-030 territory),
    and a request is past both by the time a reroute decision happens
    post-acceptance (it is `IN_FULFILMENT`). `next_seq` is computed fresh here (COUNT of this
    request's existing orders + 1, seen inside the same transaction as any
    orders this call itself is about to insert) rather than threaded in by
    the caller, so both call sites — the checkout loop below and a lone
    remainder allocation — get a correct, gap-free order number."""
    vendor = session.execute(text("SELECT * FROM vendor_account WHERE id=:v"), {"v": vendor_id}).mappings().one()
    product_ids = [l["index_product_id"] for l in vendor_lines]
    products = {
        row["id"]: row for row in session.execute(
            text("SELECT id, regulated_price FROM index_product WHERE id = ANY(:ids)"), {"ids": product_ids}
        ).mappings().all()
    }

    goods_total = Decimal("0")
    line_payloads = []
    for line in vendor_lines:
        product = products[line["index_product_id"]]
        offer_info = chosen_offer[line["id"]]
        if product["regulated_price"]:
            ref = session.execute(
                text(
                    "SELECT id, wholesale_derived_price FROM price_reference WHERE index_product_id=:p "
                    "AND effective_from <= :today ORDER BY effective_from DESC LIMIT 1"
                ),
                {"p": line["index_product_id"], "today": date.today()},
            ).mappings().first()
            if ref is None:
                raise ApiError("GUARD_FAILED", "no price reference for regulated product", rule="R-006")
            unit_price = ref["wholesale_derived_price"]
            price_source, price_source_id = "PRICE_REFERENCE", ref["id"]
        else:
            unit_price = offer_info["price"]
            price_source, price_source_id = "VENDOR_OFFER", offer_info["offer_id"]
        goods_total += unit_price * line["qty_requested"]
        line_payloads.append((line, product, unit_price, price_source, price_source_id))

    payment_terms = "UPFRONT"
    credit_days = None
    if payment_overrides.get(vendor_id) == "UPFRONT":
        pass  # J-20 UPFRONT: the pharmacy chose to bypass the credit facility entirely
    else:
        facility = session.execute(
            text("SELECT * FROM credit_facility WHERE vendor_id=:v AND pharmacy_id=:p"),
            {"v": vendor_id, "p": req["pharmacy_id"]},
        ).mappings().first()
        if facility:
            gate = credit_check(session, facility["id"], vendor_id, req["pharmacy_id"], goods_total)
            if gate.blocked:
                raise ApiError("GUARD_FAILED", "credit limit exceeded for this pharmacy/vendor pair",
                                rule="R-044", details=[{"field": "options", "reason": ",".join(gate.options)}])
            payment_terms = "CREDIT_N_DAYS"
            credit_days = facility["terms_days"]

    next_seq = session.execute(
        text('SELECT COUNT(*) + 1 FROM "order" WHERE request_id=:r'), {"r": req["id"]}
    ).scalar()
    order_id = new_id("ord")
    order_number = f"{req['number']}-V{next_seq:02d}"
    session.execute(
        text(
            "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
            "credit_days, delivery_mode, goods_total) VALUES "
            "(:id, :num, :rid, :vid, :pid, 'PENDING_ACCEPTANCE', :terms, :days, :dmode, :total)"
        ),
        {"id": order_id, "num": order_number, "rid": req["id"], "vid": vendor_id, "pid": req["pharmacy_id"],
         "terms": payment_terms, "days": credit_days, "dmode": vendor["delivery_mode"], "total": goods_total},
    )
    # A8.1 / A4.2: SlaTimer(ACCEPTANCE) starts as soon as the order
    # exists; SM-20's one open eta_estimate per order (R-158) does too.
    timers.start(session, policy_type="ACCEPTANCE", subject_type="order", subject_id=order_id,
                 cfg_key="CFG-SLA-ACCEPTANCE-HOURS", vendor_id=vendor_id)
    create_provisional(session, order_id)

    for line, product, unit_price, price_source, price_source_id in line_payloads:
        session.execute(
            text(
                "INSERT INTO order_line (id, order_id, request_line_id, index_product_id, regulated_price, "
                "ordered_qty, unit_price, price_source, price_source_id) VALUES "
                "(:id, :oid, :rlid, :pid, :reg, :qty, :price, :src, :srcid)"
            ),
            {"id": new_id("orl"), "oid": order_id, "rlid": line["id"], "pid": line["index_product_id"],
             "reg": product["regulated_price"], "qty": line["qty_requested"], "price": unit_price,
             "src": price_source, "srcid": price_source_id},
        )
        session.execute(
            text(
                "INSERT INTO allocation (id, request_line_id, vendor_id, reason_code, strategy, "
                "sequence_snapshot, inputs_fingerprint) VALUES "
                "(:id, :rlid, :vid, 'RANKING_DEFAULT', :strategy, CAST(:snap AS JSONB), :fp)"
            ),
            {"id": new_id("alc"), "rlid": line["id"], "vid": vendor_id, "strategy": req["allocation_strategy"],
             "snap": __import__("json").dumps(chosen_offer[line["id"]]["snapshot"], default=str),
             "fp": chosen_offer[line["id"]]["fingerprint"]},
        )

    schedules = active_schedules_for(session, vendor_id=vendor_id, pharmacy_id=req["pharmacy_id"],
                                      region_code=pharmacy["region_code"], today=date.today())
    pharmacy_fee = None
    for schedule in schedules:
        if schedule["payer"] == "PHARMACY":
            consent_ok = session.execute(
                text(
                    "SELECT 1 FROM consent_record c JOIN terms_version t ON t.id = c.terms_version_id "
                    "WHERE c.subject_type='PHARMACY_ACCOUNT' AND c.subject_id=:p AND c.withdrawn_at IS NULL "
                    "AND t.mentions_pharmacy_service_fee AND t.service_fee_amount = :amt"
                ),
                {"p": req["pharmacy_id"], "amt": schedule["rate_or_amount"]},
            ).first()
            if not consent_ok:
                continue  # R-147: no consent -> no pharmacy fee captured
            # "Also fix" item: fee_schedule.rate_or_amount is NUMERIC(14,4)
            # (it doubles as a percentage rate for other fee types), so a
            # bare str() on it gave "150.0000" here against the order
            # response's "150.00" (fee_event.amount, NUMERIC(14,2), via
            # fulfilment/router.py::_order_with_lines) for the same MZN
            # figure. A PHARMACY_SERVICE_FEE's rate_or_amount is a flat
            # money amount, not a rate, so money_str's 2dp quantize is the
            # right, and now consistent, serialisation for it.
            pharmacy_fee = {"label_pt": "taxa de serviço", "amount": money_str(schedule["rate_or_amount"])}
        session.execute(
            text("INSERT INTO order_fee_schedule (order_id, fee_schedule_id) VALUES (:o, :f) "
                 "ON CONFLICT DO NOTHING"),
            {"o": order_id, "f": schedule["id"]},
        )

    return {"id": order_id, "number": order_number, "vendor_id": vendor_id,
            "goods_total": str(goods_total), "payment_terms": payment_terms,
            "platform_fees": [pharmacy_fee] if pharmacy_fee else []}


def allocate_remainder(session: Session, *, request_line_id: str, actor) -> dict:
    """Defect 4 fix. SM-04's `AUTO_REROUTE`/`PHARMACY_REROUTE`/
    `ACCEPT_SUBSTITUTE` effects (`sm04_order_line.py::_effect_reroute`)
    insert the remainder `request_line` (`origin_line_id` set) but never
    allocate it — zero `allocation` rows, no second `order` — despite that
    function's docstring claiming it "re-enters the ordinary checkout path",
    which is false: `checkout()` enters only from `DRAFT` or `CONFIRMED`, and
    by the time a line is short post-acceptance the request is `IN_FULFILMENT`.
    This is the real re-entry point, called by
    `POST /v1/request-lines/{id}/allocate`: it ranks the remainder quantity
    — excluding the vendor whose shortfall produced it (R-029) — and, if a
    fresh candidate exists, creates a new single-vendor, single-line `order`
    for it via `_create_order_for_vendor` (same shape `checkout()` itself
    produces). If no fresh candidate exists, nothing is silently lost: the
    line is marked `UNRESOLVED` so it stays visible in
    `GET /v1/requests/{id}` (its `lines` list is unfiltered by
    `origin_line_id`) instead of sitting allocated-nowhere with no signal."""
    line = session.execute(text("SELECT * FROM request_line WHERE id=:id"), {"id": request_line_id}).mappings().first()
    if line is None:
        raise ApiError("NOT_FOUND", "request line not found")
    if line["origin_line_id"] is None:
        raise ApiError("GUARD_FAILED", "not a rerouted remainder line")
    already = session.execute(
        text("SELECT 1 FROM allocation WHERE request_line_id=:id"), {"id": request_line_id}
    ).first()
    if already is not None:
        raise ApiError("GUARD_FAILED", "this line is already allocated")

    req = session.execute(text("SELECT * FROM request WHERE id=:r"), {"r": line["request_id"]}).mappings().one()
    pharmacy = session.execute(
        text("SELECT region_code FROM pharmacy_account WHERE id=:p"), {"p": req["pharmacy_id"]}
    ).mappings().one()

    origin_vendor_id = session.execute(
        text(
            'SELECT o.vendor_id FROM order_line ol JOIN "order" o ON o.id = ol.order_id '
            "WHERE ol.request_line_id = :origin"
        ),
        {"origin": line["origin_line_id"]},
    ).scalar()
    excluded = frozenset({origin_vendor_id}) if origin_vendor_id else frozenset()

    result = rank(build_ranking_input(
        session, index_product_id=line["index_product_id"], qty_requested=line["qty_requested"],
        pharmacy_id=req["pharmacy_id"], strategy=req["allocation_strategy"], excluded_vendor_ids=excluded,
    ))
    if not result.ordered:
        session.execute(
            text("UPDATE request_line SET match_status='UNRESOLVED' WHERE id=:id"), {"id": request_line_id}
        )
        return {"request_line_id": request_line_id, "match_status": "UNRESOLVED", "order": None}

    top = result.ordered[0]
    chosen_offer = {
        request_line_id: {"offer_id": top.offer_id, "price": top.price, "snapshot": result.snapshot,
                           "fingerprint": result.inputs_fingerprint}
    }
    order_info = _create_order_for_vendor(
        session, req=req, pharmacy=pharmacy, vendor_id=top.vendor_id, vendor_lines=[dict(line)],
        chosen_offer=chosen_offer, payment_overrides={},
    )
    session.execute(text("UPDATE request_line SET match_status='RESOLVED' WHERE id=:id"), {"id": request_line_id})
    return {"request_line_id": request_line_id, "match_status": "RESOLVED", "order": order_info}


def checkout(session: Session, *, request_id: str, actor, payment_overrides: dict[str, str] | None = None) -> dict:
    """`payment_overrides`: `{vendor_id: "UPFRONT"}` — the J-20 minimum this
    session adds (item 3, backend-review-r1.md): when the credit gate blocks
    a vendor's sub-basket, the pharmacy can force that vendor's order onto
    `UPFRONT` terms instead, which needs no credit_facility headroom at all.
    Without this a pharmacy at its limit was simply stuck — the four options
    were *reported* by `credit/gate.py` but none could be acted on."""
    payment_overrides = payment_overrides or {}
    req = session.execute(text("SELECT * FROM request WHERE id=:r"), {"r": request_id}).mappings().first()
    if req is None:
        raise ApiError("NOT_FOUND", "request not found")
    # SM-01 has two lanes into CONFIRMED and both have to be able to buy:
    #
    #   catalogue   DRAFT --SUBMIT_CART--> CONFIRMED
    #               DRAFT --SUBMIT_CART_OVER_THRESHOLD--> AWAITING_ADMIN_APPROVAL
    #                     --ADMIN_APPROVE--> CONFIRMED
    #   intake      NORMALIZING --NORMALIZATION_COMPLETE--> AWAITING_CONFIRMATION
    #                     --CONFIRM--> CONFIRMED
    #
    # Accepting DRAFT alone left the second and third of those as dead ends:
    # a WhatsApp list the pharmacy had already resolved and confirmed, and a
    # cart the pharmacy admin had already approved, both sat at CONFIRMED
    # with no route to an order. Found against the live pilot server, not in
    # a test — every unit test drove the DRAFT lane.
    #
    # CONFIRMED is accepted, AWAITING_CONFIRMATION is not: confirming is the
    # buyer's own act (SM-01 CONFIRM, guarded by the confirmation timer and
    # restricted to PharmacyBuyer/OpsReviewer), and checkout must not perform
    # it on their behalf — a PharmacyAdmin could otherwise buy a basket the
    # buyer never confirmed.
    entry_status = req["status"]
    if entry_status not in ("DRAFT", "CONFIRMED"):
        raise ApiError(
            "GUARD_FAILED",
            f"request is in {entry_status}; checkout accepts DRAFT or CONFIRMED",
            rule="SM-01",
        )

    lines = session.execute(
        text("SELECT * FROM request_line WHERE request_id=:r"), {"r": request_id}
    ).mappings().all()
    if not lines:
        raise ApiError("GUARD_FAILED", "request has no lines")

    pharmacy = session.execute(
        text("SELECT region_code FROM pharmacy_account WHERE id=:p"), {"p": req["pharmacy_id"]}
    ).mappings().one()

    by_vendor: dict[str, list[dict]] = {}
    chosen_offer: dict[str, dict] = {}
    for line in lines:
        result = rank(build_ranking_input(
            session, index_product_id=line["index_product_id"], qty_requested=line["qty_requested"],
            pharmacy_id=req["pharmacy_id"], strategy=req["allocation_strategy"],
        ))
        if not result.ordered:
            raise ApiError("GUARD_FAILED", f"no fresh offer for line {line['id']}", rule="R-014")
        top = result.ordered[0]
        by_vendor.setdefault(top.vendor_id, []).append(dict(line))
        chosen_offer[line["id"]] = {"offer_id": top.offer_id, "price": top.price, "snapshot": result.snapshot,
                                     "fingerprint": result.inputs_fingerprint}

    orders_out = []
    for vendor_id, vendor_lines in by_vendor.items():
        orders_out.append(_create_order_for_vendor(
            session, req=req, pharmacy=pharmacy, vendor_id=vendor_id, vendor_lines=vendor_lines,
            chosen_offer=chosen_offer, payment_overrides=payment_overrides,
        ))

    machine = MACHINES["SM-01"]
    if entry_status == "DRAFT":
        # R-122's threshold guard lives inside this transition, so the
        # catalogue lane still cannot skip the admin-approval gate.
        machine.apply(session, request_id, "SUBMIT_CART", actor)
    machine.apply(session, request_id, "FIRST_ORDER_CREATED", "SYSTEM")

    return {"request_id": request_id, "orders": orders_out}
