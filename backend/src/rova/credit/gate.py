"""A10 — the credit gate (R-038...R-044)."""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.credit.exposure import current_exposure, pending_exposure


@dataclass(frozen=True)
class CreditGateResult:
    blocked: bool
    current_exposure: Decimal
    pending_exposure: Decimal
    headroom: Decimal
    options: list[str]


def check(session: Session, facility_id: str, vendor_id: str, pharmacy_id: str, order_value: Decimal) -> CreditGateResult:
    facility = session.execute(
        text("SELECT limit_amount, status, terms_days FROM credit_facility WHERE id=:f"), {"f": facility_id}
    ).mappings().one()

    cur = current_exposure(session, facility_id)
    pend = pending_exposure(session, vendor_id, pharmacy_id)
    headroom = facility["limit_amount"] - cur - pend

    blocked = facility["status"] == "SUSPENDED" or (cur + pend + order_value > facility["limit_amount"])

    options = []
    if blocked:
        options.append("UPFRONT")
        if facility["status"] != "SUSPENDED" and facility["terms_days"] and headroom > 0:
            options.append("SPLIT")
        options.append("REROUTE")
        options.append("ADMIN_OVERRIDE")

    return CreditGateResult(blocked=blocked, current_exposure=cur, pending_exposure=pend,
                             headroom=headroom, options=options)
