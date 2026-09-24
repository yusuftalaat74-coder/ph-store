"""Asking for a credit limit to be exceeded, and spending that permission.

The gate (`credit/gate.py`) decides whether a sub-basket fits inside a
pharmacy's facility. When it does not, the answer used to be the end of the
conversation: R-044, "credit limit exceeded", and a pharmacist standing
behind a counter with a full basket and nothing to press. The gate itself
has always listed `ADMIN_OVERRIDE` among the options — this is that option,
finally attached to something.

Three rules the shape enforces rather than documents:

* **One approval, one order.** The row is consumed when the order is written
  (`status='USED'` with the order's id on it). An approval that could be
  spent twice is a limit raise wearing a different name.

* **It covers an amount, not a pharmacy.** The basket is priced at the
  moment of asking and that figure is the ceiling. If the pharmacist adds
  more afterwards, the gate refuses again — which is right, because nobody
  approved the larger number.

* **It expires.** `CFG-CREDIT-OVERRIDE-HOURS` (48). An approval nobody spent
  is headroom the credit team stopped thinking about days ago, and it should
  not still be live when they next look at the account.

Either side may decide. The distributor extends the credit and the platform
carries the relationship, so both have a real claim on the decision — and
`decided_by_side` records which, at the time, rather than being inferred
later from memberships that will have changed.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain.enums import RoleCode

# Who may answer. The pharmacy is deliberately absent: a facility a pharmacy
# could raise for itself is not a facility.
VENDOR_DECIDERS = frozenset({RoleCode.VENDOR_ADMIN, RoleCode.VENDOR_ORDER_DESK,
                             RoleCode.VENDOR_FINANCE})
PLATFORM_DECIDERS = frozenset({RoleCode.PLATFORM_ADMIN, RoleCode.PLATFORM_FINANCE,
                               RoleCode.OPS_REVIEWER})
DECIDERS = VENDOR_DECIDERS | PLATFORM_DECIDERS


def side_for(principal) -> str | None:
    """Which side of the table this person is answering from.

    A vendor membership answers as the vendor even if the person also holds a
    platform role, because the vendor is the one whose money is at risk and
    that is the stronger claim to have made the decision.
    """
    roles = set(principal.roles)
    if roles & VENDOR_DECIDERS and principal.vendor_id:
        return "VENDOR"
    if roles & PLATFORM_DECIDERS:
        return "PLATFORM"
    if roles & VENDOR_DECIDERS:
        return "VENDOR"
    return None


def usable(session: Session, *, request_id: str, vendor_id: str,
           amount: Decimal) -> dict | None:
    """The approval that would let this exact sub-basket through, if there is
    one: approved, not yet spent, not expired, and for at least this much."""
    return session.execute(
        text("SELECT * FROM credit_override WHERE request_id=:r AND vendor_id=:v "
             "AND status='APPROVED' AND expires_at > :now AND amount_requested >= :amt"),
        {"r": request_id, "v": vendor_id, "now": now(), "amt": amount},
    ).mappings().first()


def spend(session: Session, override_id: str, order_id: str) -> None:
    """Consume it. Called from inside the same transaction that writes the
    order, so an order on an overridden limit and the record of who allowed
    it either both exist or neither does."""
    session.execute(
        text("UPDATE credit_override SET status='USED', used_order_id=:o, used_at=:n, "
             "updated_at=:n WHERE id=:i AND status='APPROVED'"),
        {"o": order_id, "n": now(), "i": override_id},
    )


def open_request(session: Session, *, request_id: str, vendor_id: str, pharmacy_id: str,
                 facility_id: str, amount: Decimal, headroom: Decimal, exposure: Decimal,
                 user_id: str, note: str | None) -> dict:
    hours = float(cfg.get(session, "CFG-CREDIT-OVERRIDE-HOURS", default=48))
    oid = new_id("cov")
    session.execute(
        text("INSERT INTO credit_override (id, request_id, vendor_id, pharmacy_id, facility_id, "
             "amount_requested, headroom_at_request, exposure_at_request, note, "
             "requested_by_user_id, expires_at) "
             "VALUES (:i, :r, :v, :p, :f, :amt, :head, :exp, :note, :u, :until)"),
        {"i": oid, "r": request_id, "v": vendor_id, "p": pharmacy_id, "f": facility_id,
         "amt": amount, "head": headroom, "exp": exposure, "note": note, "u": user_id,
         "until": now() + timedelta(hours=hours)},
    )
    return read(session, oid)


def decide(session: Session, *, override_id: str, approve: bool, principal,
           reason: str | None) -> dict:
    row = session.execute(
        text("SELECT * FROM credit_override WHERE id=:i"), {"i": override_id},
    ).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "credit override request not found")

    side = side_for(principal)
    if side is None:
        raise ApiError("FORBIDDEN", "this decision belongs to the distributor or to PH Store")
    # A vendor person answers for their own vendor and nobody else's.
    if side == "VENDOR" and principal.vendor_id and principal.vendor_id != row["vendor_id"]:
        raise ApiError("NOT_FOUND", "credit override request not found")

    if row["status"] != "PENDING":
        raise ApiError("GUARD_FAILED",
                       f"this request is already {row['status'].lower()}", rule="R-044")
    if row["expires_at"] <= now():
        session.execute(
            text("UPDATE credit_override SET status='EXPIRED', updated_at=:n WHERE id=:i"),
            {"n": now(), "i": override_id})
        raise ApiError("GUARD_FAILED",
                       "this request expired before it was answered", rule="R-044")

    session.execute(
        text("UPDATE credit_override SET status=:st, decided_by_user_id=:u, decided_by_side=:side, "
             "decided_at=:n, decision_reason=:why, updated_at=:n WHERE id=:i"),
        {"st": "APPROVED" if approve else "DECLINED", "u": principal.user_id,
         "side": side, "n": now(), "why": reason, "i": override_id},
    )
    return read(session, override_id)


def read(session: Session, override_id: str) -> dict:
    row = session.execute(
        text("SELECT co.*, v.trade_name AS vendor_name, p.trade_name AS pharmacy_name "
             "FROM credit_override co "
             "JOIN vendor_account v ON v.id = co.vendor_id "
             "JOIN pharmacy_account p ON p.id = co.pharmacy_id "
             "WHERE co.id=:i"),
        {"i": override_id},
    ).mappings().one()
    return _shape(row)


def _shape(row) -> dict:
    from rova.core.money import money_str

    return {
        "id": row["id"], "request_id": row["request_id"],
        "vendor_id": row["vendor_id"], "vendor_name": row["vendor_name"],
        "pharmacy_id": row["pharmacy_id"], "pharmacy_name": row["pharmacy_name"],
        "status": row["status"],
        "amount_requested": money_str(row["amount_requested"]),
        "headroom_at_request": money_str(row["headroom_at_request"]),
        "exposure_at_request": money_str(row["exposure_at_request"]),
        "note": row["note"],
        "requested_by_user_id": row["requested_by_user_id"],
        "requested_at": row["requested_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "decided_by_user_id": row["decided_by_user_id"],
        "decided_by_side": row["decided_by_side"],
        "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
        "decision_reason": row["decision_reason"],
        "used_order_id": row["used_order_id"],
    }


def listing(session: Session, principal, *, status: str | None, limit: int) -> list[dict]:
    """What this caller may see: a pharmacy sees its own asks, a vendor sees
    the ones pointed at it, and the platform sees all of them."""
    where = ["1=1"]
    params: dict = {"lim": limit}
    if principal.pharmacy_id:
        where.append("co.pharmacy_id = :pha")
        params["pha"] = principal.pharmacy_id
    elif principal.vendor_id:
        where.append("co.vendor_id = :ven")
        params["ven"] = principal.vendor_id
    elif not (set(principal.roles) & PLATFORM_DECIDERS):
        return []
    if status:
        where.append("co.status = :st")
        params["st"] = status

    rows = session.execute(
        text("SELECT co.*, v.trade_name AS vendor_name, p.trade_name AS pharmacy_name "
             "FROM credit_override co "
             "JOIN vendor_account v ON v.id = co.vendor_id "
             "JOIN pharmacy_account p ON p.id = co.pharmacy_id "
             "WHERE " + " AND ".join(where) +
             " ORDER BY co.requested_at DESC LIMIT :lim"),
        params,
    ).mappings().all()
    return [_shape(r) for r in rows]
