"""A3.3 — derived credit values, never stored, always computed here."""
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

# R-039 order states that count toward pending exposure (six states, per A3.3)
PENDING_EXPOSURE_STATES = (
    "PENDING_ACCEPTANCE", "ACCEPTED", "DISPATCHED", "DELIVERED_PENDING_RECEIPT",
    "RECEIPT_ACCEPTED", "DISPUTED",
)


def current_exposure(session: Session, facility_id: str) -> Decimal:
    """R-038: opening_balance + sum of signed ledger_entry.amount (ADJUSTMENT included)."""
    opening = session.execute(
        text("SELECT opening_balance FROM credit_facility WHERE id=:f"), {"f": facility_id}
    ).scalar()
    ledger_sum = session.execute(
        text("SELECT COALESCE(SUM(amount), 0) FROM ledger_entry WHERE credit_facility_id=:f"), {"f": facility_id}
    ).scalar()
    return Decimal(opening) + Decimal(ledger_sum)


def pending_exposure(session: Session, vendor_id: str, pharmacy_id: str) -> Decimal:
    """R-039: orders in the six non-terminal-for-credit states, using
    ordered_qty x unit_price before acceptance and confirmed_qty x unit_price
    after, excluding any order that already has an OK-matched invoice."""
    row = session.execute(
        text(
            """
            SELECT COALESCE(SUM(
                CASE WHEN o.status = 'PENDING_ACCEPTANCE'
                     THEN ol.ordered_qty * ol.unit_price
                     ELSE ol.confirmed_qty * ol.unit_price
                END
            ), 0) AS total
            FROM "order" o
            JOIN order_line ol ON ol.order_id = o.id
            WHERE o.vendor_id = :v AND o.pharmacy_id = :p
              AND o.status = ANY(:states)
              AND NOT EXISTS (
                  SELECT 1 FROM invoice i WHERE i.order_id = o.id AND i.price_match_flag = 'OK'
              )
            """
        ),
        {"v": vendor_id, "p": pharmacy_id, "states": list(PENDING_EXPOSURE_STATES)},
    ).mappings().one()
    return Decimal(row["total"])
