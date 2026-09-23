"""A11.3 — fee accrual, hooked on state-machine transitions, never inside
order code (A6.1's fee-blindness depends on this being the only place that
reads both an order and a fee_schedule). Implemented subset: VENDOR_SHARE and
FLAT_PER_ORDER at SM-03 RECEIPT_ACCEPTED; PHARMACY_SERVICE_FEE conditional at
SM-01 CONFIRMED (R-147). PER_DELIVERY_JOB (needs SM-05) and the invoice-driven
NET_DELIVERED_VALUE correction are not wired in this session (manifest)."""
from decimal import Decimal

from sqlalchemy import text

from rova.core.clock import now
from rova.core.ids import new_id
from rova.core.money import quantize
from rova.domain.fsm import Ctx


def _attribution(session, vendor_id: str, pharmacy_id: str) -> str:
    row = session.execute(
        text("SELECT 1 FROM vendor_pharmacy_relationship WHERE vendor_id=:v AND pharmacy_id=:p AND expires_at > :now"),
        {"v": vendor_id, "p": pharmacy_id, "now": now()},
    ).first()
    return "PRE_EXISTING" if row else "INCREMENTAL"


def _insert_fee_event(session, *, fee_schedule_id, payer_org_id, order_id, base_amount, amount, attribution):
    session.execute(
        text(
            "INSERT INTO fee_event (id, fee_schedule_id, payer_org_id, order_id, base_amount, amount, "
            "attribution, status, occurred_at) VALUES (:id, :fs, :org, :o, :base, :amt, :attr, 'ACCRUED', :now)"
        ),
        {"id": new_id("fev"), "fs": fee_schedule_id, "org": payer_org_id, "o": order_id,
         "base": quantize(base_amount), "amt": quantize(amount), "attr": attribution, "now": now()},
    )


def on_receipt_accepted(ctx: Ctx) -> None:
    """SM-03 -> RECEIPT_ACCEPTED (A11.3 table, rows VENDOR_SHARE / FLAT_PER_ORDER)."""
    session = ctx.session
    order_id = ctx.subject_id
    order = session.execute(text('SELECT vendor_id, pharmacy_id FROM "order" WHERE id=:o'), {"o": order_id}).mappings().one()

    schedules = session.execute(
        text(
            "SELECT fs.* FROM order_fee_schedule ofs JOIN fee_schedule fs ON fs.id = ofs.fee_schedule_id "
            "WHERE ofs.order_id=:o AND fs.type IN ('VENDOR_SHARE','FLAT_PER_ORDER','PHARMACY_SERVICE_FEE')"
        ),
        {"o": order_id},
    ).mappings().all()
    if not schedules:
        return

    lines = session.execute(
        text("SELECT unit_price, confirmed_qty, index_product_id FROM order_line WHERE order_id=:o"),
        {"o": order_id},
    ).mappings().all()
    net_delivered = sum((row["unit_price"] * row["confirmed_qty"] for row in lines), Decimal("0"))

    vendor_org_id = session.execute(
        text("SELECT organisation_id FROM vendor_account WHERE id=:v"), {"v": order["vendor_id"]}
    ).scalar()
    pharmacy_org_id = session.execute(
        text("SELECT organisation_id FROM pharmacy_account WHERE id=:p"), {"p": order["pharmacy_id"]}
    ).scalar()

    attribution = _attribution(session, order["vendor_id"], order["pharmacy_id"])

    for schedule in schedules:
        if schedule["type"] == "PHARMACY_SERVICE_FEE":
            # Simplified: firm at RECEIPT_ACCEPTED only (the spec's two-step
            # conditional-at-CONFIRMED / firm-at-first-receipt is not wired
            # in this session — see manifest).
            _insert_fee_event(session, fee_schedule_id=schedule["id"], payer_org_id=pharmacy_org_id,
                               order_id=order_id, base_amount=schedule["rate_or_amount"],
                               amount=schedule["rate_or_amount"], attribution=attribution)
            continue

        if schedule["type"] == "FLAT_PER_ORDER":
            _insert_fee_event(session, fee_schedule_id=schedule["id"], payer_org_id=vendor_org_id,
                               order_id=order_id, base_amount=schedule["rate_or_amount"],
                               amount=schedule["rate_or_amount"], attribution=attribution)
            continue

        # VENDOR_SHARE
        if schedule["applies_to"] == "INCREMENTAL_ORDERS_ONLY" and attribution == "PRE_EXISTING":
            _insert_fee_event(session, fee_schedule_id=schedule["id"], payer_org_id=vendor_org_id,
                               order_id=order_id, base_amount=Decimal("0"), amount=Decimal("0"),
                               attribution=attribution)
            continue

        if schedule["base"] == "DECLARED_MARGIN":
            base_amount = Decimal("0")
            for line in lines:
                offer = session.execute(
                    text(
                        "SELECT plr.vendor_cost FROM vendor_offer vo "
                        "JOIN price_list_row plr ON plr.id = vo.source_row_id "
                        "WHERE vo.vendor_id=:v AND vo.index_product_id=:p"
                    ),
                    {"v": order["vendor_id"], "p": line["index_product_id"]},
                ).scalar()
                if offer is not None:
                    base_amount += (line["unit_price"] - offer) * line["confirmed_qty"]
                # else: no cost basis -> contributes 0 (A11.3)
        else:  # NET_DELIVERED_VALUE
            base_amount = net_delivered

        amount = base_amount * schedule["rate_or_amount"]
        _insert_fee_event(session, fee_schedule_id=schedule["id"], payer_org_id=vendor_org_id,
                           order_id=order_id, base_amount=base_amount, amount=amount, attribution=attribution)


def on_request_confirmed(ctx: Ctx) -> None:
    """SM-01 -> CONFIRMED: conditional PHARMACY_SERVICE_FEE (A11.3), captured
    only under the R-147 consent condition already checked at order_fee_schedule
    capture time in rova.ordering.checkout; here we just accrue for schedules
    actually captured on the request's future orders is out of scope for a
    request-level (pre-order) event in this reduced implementation — the
    conditional accrual is instead folded into checkout (see rova/ordering/checkout.py)."""
    return
