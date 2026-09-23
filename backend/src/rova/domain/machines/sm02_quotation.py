"""SM-02 — quotation (§6 SM-02, A4.2 row SM-02).

Implemented subset: the whole trigger vocabulary is wired (`SUBMIT`,
`QUOTE_TIMEOUT`, `ACCEPT_LINES` with the R-122 buyer/admin split,
`DECLINE`, `EXPIRE_UNANSWERED`). `ACCEPT_LINES` creates the SM-03 `Order`
`+` `order_line`s `+` `allocation` rows directly here (mirroring
`rova/ordering/checkout.py`'s INSERT pattern for the CATALOGUE path) since
the RFQ path has no separate checkout step to run afterwards (§8.4).
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import text

from rova.core.clock import now
from rova.core.ids import new_id
from rova.domain import timers
from rova.domain.enums import QuotationStatus, RoleCode
from rova.domain.fsm import Ctx, GuardResult, Machine, Transition

_VENDOR_DESK = frozenset({RoleCode.VENDOR_ORDER_DESK, RoleCode.VENDOR_ADMIN})
_BUYER = frozenset({RoleCode.PHARMACY_BUYER, RoleCode.PHARMACY_ADMIN})
_ADMIN = frozenset({RoleCode.PHARMACY_ADMIN})

SUBJECT = "quotation"
POLICY = "QUOTATION"


def _guard_submit(ctx: Ctx) -> GuardResult:
    if not timers.is_running(ctx.session, policy_type=POLICY, subject_type=SUBJECT, subject_id=ctx.subject_id):
        return GuardResult.failed("SlaTimer(quote) is not running (already expired)", rule="A-04")
    return GuardResult.passed()


def _accepted_lines(ctx: Ctx) -> list[dict]:
    return ctx.kwargs.get("accept_lines", [])


def _guard_accept(ctx: Ctx) -> GuardResult:
    if ctx.subject_row["expires_at"] <= now():
        return GuardResult.failed("quotation has expired", rule="R-122")
    lines = _accepted_lines(ctx)
    if not lines:
        return GuardResult.failed("at least one line must be accepted", rule="R-122")
    threshold = ctx.session.execute(
        text(
            "SELECT pa.buyer_approval_threshold FROM pharmacy_account pa JOIN request r ON r.pharmacy_id = pa.id "
            "WHERE r.id = :r"
        ),
        {"r": ctx.subject_row["request_id"]},
    ).scalar()
    if threshold is None:
        return GuardResult.passed()
    total = Decimal("0")
    for entry in lines:
        row = ctx.session.execute(
            text("SELECT price, offered_qty FROM quotation_line WHERE id=:id AND quotation_id=:q"),
            {"id": entry["quotation_line_id"], "q": ctx.subject_id},
        ).mappings().one()
        unit_price = row["price"] if row["price"] is not None else Decimal("0")
        total += unit_price * entry.get("accepted_qty", row["offered_qty"])
    actor_roles = ctx.actor.roles if ctx.actor != "SYSTEM" else frozenset()
    if total > threshold and RoleCode.PHARMACY_ADMIN not in actor_roles:
        return GuardResult.failed(
            "accepted total exceeds PharmacyAccount.buyer_approval_threshold; only PharmacyAdmin may confirm",
            rule="R-122",
        )
    return GuardResult.passed()


def _effect_submit(ctx: Ctx) -> None:
    timers.cancel(ctx.session, policy_type=POLICY, subject_type=SUBJECT, subject_id=ctx.subject_id)


def _effect_accept(ctx: Ctx) -> None:
    session = ctx.session
    quotation = ctx.subject_row
    request = session.execute(text("SELECT * FROM request WHERE id=:r"), {"r": quotation["request_id"]}).mappings().one()
    vendor = session.execute(
        text("SELECT * FROM vendor_account WHERE id=:v"), {"v": quotation["vendor_id"]}
    ).mappings().one()

    order_id = new_id("ord")
    next_seq = session.execute(
        text('SELECT COUNT(*) + 1 FROM "order" WHERE request_id=:r'), {"r": request["id"]}
    ).scalar()
    order_number = f"{request['number']}-V{next_seq:02d}"
    goods_total = Decimal("0")
    line_rows = []
    for entry in _accepted_lines(ctx):
        qline = session.execute(
            text("SELECT * FROM quotation_line WHERE id=:id AND quotation_id=:q"),
            {"id": entry["quotation_line_id"], "q": ctx.subject_id},
        ).mappings().one()
        accepted_qty = entry.get("accepted_qty", qline["offered_qty"])
        if qline["regulated_price"]:
            ref = session.execute(
                text(
                    "SELECT id, wholesale_derived_price FROM price_reference WHERE index_product_id=:p "
                    "AND effective_from <= :today ORDER BY effective_from DESC LIMIT 1"
                ),
                {"p": qline["index_product_id"], "today": date.today()},
            ).mappings().one()
            unit_price, price_source, price_source_id = ref["wholesale_derived_price"], "PRICE_REFERENCE", ref["id"]
        else:
            unit_price, price_source, price_source_id = qline["price"], "QUOTATION", qline["id"]
        goods_total += unit_price * accepted_qty
        line_rows.append((qline, accepted_qty, unit_price, price_source, price_source_id))
        session.execute(text("UPDATE quotation_line SET accepted=true, updated_at=:n WHERE id=:id"),
                         {"n": now(), "id": qline["id"]})

    session.execute(
        text(
            "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
            "delivery_mode, goods_total) VALUES (:id, :num, :rid, :vid, :pid, 'PENDING_ACCEPTANCE', 'UPFRONT', "
            ":dmode, :total)"
        ),
        {"id": order_id, "num": order_number, "rid": request["id"], "vid": quotation["vendor_id"],
         "pid": request["pharmacy_id"], "dmode": vendor["delivery_mode"], "total": goods_total},
    )
    for qline, accepted_qty, unit_price, price_source, price_source_id in line_rows:
        session.execute(
            text(
                "INSERT INTO order_line (id, order_id, request_line_id, index_product_id, regulated_price, "
                "ordered_qty, unit_price, price_source, price_source_id) VALUES "
                "(:id, :oid, :rlid, :pid, :reg, :qty, :price, :src, :srcid)"
            ),
            {"id": new_id("orl"), "oid": order_id, "rlid": qline["request_line_id"], "pid": qline["index_product_id"],
             "reg": qline["regulated_price"], "qty": accepted_qty, "price": unit_price,
             "src": price_source, "srcid": price_source_id},
        )
        session.execute(
            text(
                "INSERT INTO allocation (id, request_line_id, vendor_id, reason_code, strategy, sequence_snapshot, "
                "inputs_fingerprint) VALUES (:id, :rlid, :vid, 'RANKING_DEFAULT', :strategy, "
                "CAST('[]' AS JSONB), 'rfq-accept')"
            ),
            {"id": new_id("alc"), "rlid": qline["request_line_id"], "vid": quotation["vendor_id"],
             "strategy": request["allocation_strategy"]},
        )
    # A8.1/A4.2 companions of order creation, same as rova/ordering/checkout.py
    from rova.domain.machines.sm20_eta_estimate import create_provisional
    timers.start(session, policy_type="ACCEPTANCE", subject_type="order", subject_id=order_id,
                 cfg_key="CFG-SLA-ACCEPTANCE-HOURS", vendor_id=quotation["vendor_id"])
    create_provisional(session, order_id)
    timers.cancel(ctx.session, policy_type=POLICY, subject_type=SUBJECT, subject_id=ctx.subject_id)


MACHINE = Machine(
    code="SM-02",
    subject_table=SUBJECT,
    status_column="status",
    transitions=[
        Transition("SM-02", (QuotationStatus.INVITED,), QuotationStatus.SUBMITTED, "SUBMIT",
                   _VENDOR_DESK, guard=_guard_submit, effects=_effect_submit,
                   extra_set=lambda ctx: {"submitted_at": now()}),
        Transition("SM-02", (QuotationStatus.INVITED,), QuotationStatus.EXPIRED, "QUOTE_TIMEOUT",
                   frozenset({"SYSTEM"}), rule_refs=("R-101",)),
        Transition("SM-02", (QuotationStatus.SUBMITTED,), QuotationStatus.ACCEPTED, "ACCEPT_LINES",
                   _BUYER, guard=_guard_accept, effects=_effect_accept, rule_refs=("R-122",)),
        Transition("SM-02", (QuotationStatus.SUBMITTED,), QuotationStatus.DECLINED, "DECLINE", _BUYER),
        Transition("SM-02", (QuotationStatus.SUBMITTED,), QuotationStatus.EXPIRED, "EXPIRE_UNANSWERED",
                   frozenset({"SYSTEM"})),
    ],
)
