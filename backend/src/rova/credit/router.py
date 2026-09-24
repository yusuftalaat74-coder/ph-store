"""The credit-override endpoints.

A pharmacy asks, the distributor or the platform answers, and checkout
spends the answer. The refusal these exist for is R-044, which until now was
a wall: `credit/gate.py` reported `ADMIN_OVERRIDE` as an option and there
was nothing behind it, so a pharmacist with a full basket was told his limit
was exceeded and given nothing to do about it.

Asking is deliberately not free: `POST` refuses unless the gate is actually
blocking right now. An approval the pharmacy did not need is headroom nobody
decided to give it.
"""
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.credit import override as ovr
from rova.credit.gate import check as credit_check
from rova.domain.enums import RoleCode
from rova.notifications.router import emit
from rova.ordering.pricing import price_request

router = APIRouter(prefix="/v1", tags=["credit"])

_PHARMACY = (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER)
_READERS = tuple({*_PHARMACY, *ovr.DECIDERS})


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_id: str
    # Why he needs it. Optional, because a pharmacist mid-shift should not be
    # made to write an essay to ask a question — but it is the first thing an
    # approver looks for, so the app asks for it.
    note: str | None = Field(default=None, max_length=500)


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=500)


def _vendor_subtotal(session: Session, request: dict, vendor_id: str) -> Decimal:
    """What this vendor's share of the request comes to, priced the way
    checkout will price it — the same `price_request` the cart and the
    WhatsApp confirmation screen use, so the number on the approval is the
    number that was on his screen."""
    priced = price_request(session, request)
    for bucket in priced["totals"]["by_vendor"]:
        if bucket["vendor_id"] == vendor_id:
            return Decimal(bucket["goods_total"])
    return Decimal("0")


@router.post("/requests/{request_id}/credit-override")
def ask_for_override(request_id: str, body: AskBody,
                     principal: Principal = Depends(require_roles(*_PHARMACY)),
                     session: Session = Depends(get_session, scope="function")):
    """"Ask them to let this one through."

    Refused unless the gate is blocking this vendor's share right now, so an
    approval always corresponds to a refusal somebody actually hit.
    """
    request = session.execute(
        text("SELECT * FROM request WHERE id=:r"), {"r": request_id},
    ).mappings().first()
    if request is None or request["pharmacy_id"] != principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "request not found")

    facility = session.execute(
        text("SELECT * FROM credit_facility WHERE vendor_id=:v AND pharmacy_id=:p"),
        {"v": body.vendor_id, "p": principal.pharmacy_id},
    ).mappings().first()
    if facility is None:
        raise ApiError("NOT_FOUND",
                       "this pharmacy has no credit facility with that distributor")

    amount = _vendor_subtotal(session, dict(request), body.vendor_id)
    if amount <= 0:
        raise ApiError("VALIDATION_ERROR",
                       "that distributor has nothing to supply on this request")

    gate = credit_check(session, facility["id"], body.vendor_id,
                        principal.pharmacy_id, amount)
    if not gate.blocked:
        raise ApiError("GUARD_FAILED",
                       "this order fits inside the limit; no permission is needed",
                       rule="R-044")

    existing = session.execute(
        text("SELECT * FROM credit_override WHERE request_id=:r AND vendor_id=:v "
             "AND status IN ('PENDING','APPROVED')"),
        {"r": request_id, "v": body.vendor_id},
    ).mappings().first()
    if existing is not None:
        # Not an error: he tapped twice, or a colleague asked first. Hand him
        # the one that is already open rather than a second one nobody needs.
        return ovr.read(session, existing["id"])

    out = ovr.open_request(
        session, request_id=request_id, vendor_id=body.vendor_id,
        pharmacy_id=principal.pharmacy_id, facility_id=facility["id"],
        amount=amount, headroom=gate.headroom,
        exposure=gate.current_exposure + gate.pending_exposure,
        user_id=principal.user_id, note=body.note)

    # Both sides are told, because either may answer.
    vendor_org = session.execute(
        text("SELECT organisation_id FROM vendor_account WHERE id=:v"),
        {"v": body.vendor_id}).scalar()
    payload = {"credit_override_id": out["id"], "request_id": request_id,
               "vendor_id": body.vendor_id, "pharmacy_id": principal.pharmacy_id,
               "pharmacy_name": out["pharmacy_name"],
               "amount_requested": out["amount_requested"],
               "headroom_at_request": out["headroom_at_request"]}
    emit(session, event_code="N-CREDIT-OVERRIDE-ASKED",
         recipient_org_id=vendor_org, payload=payload)
    emit(session, event_code="N-CREDIT-OVERRIDE-ASKED",
         recipient_role=RoleCode.PLATFORM_FINANCE.value, payload=payload)
    return out


@router.get("/credit-overrides")
def list_overrides(status: Literal["PENDING", "APPROVED", "DECLINED", "USED", "EXPIRED"] | None = None,
                   limit: int = Query(default=50, ge=1, le=200),
                   principal: Principal = Depends(require_roles(*_READERS)),
                   session: Session = Depends(get_session, scope="function")):
    return {"items": ovr.listing(session, principal, status=status, limit=limit),
            "next_cursor": None}


@router.post("/credit-overrides/{override_id}/approve")
def approve_override(override_id: str, body: DecisionBody,
                     principal: Principal = Depends(require_roles(*ovr.DECIDERS)),
                     session: Session = Depends(get_session, scope="function")):
    out = ovr.decide(session, override_id=override_id, approve=True,
                     principal=principal, reason=body.reason)
    _tell_the_pharmacy(session, out, approved=True)
    return out


@router.post("/credit-overrides/{override_id}/decline")
def decline_override(override_id: str, body: DecisionBody,
                     principal: Principal = Depends(require_roles(*ovr.DECIDERS)),
                     session: Session = Depends(get_session, scope="function")):
    out = ovr.decide(session, override_id=override_id, approve=False,
                     principal=principal, reason=body.reason)
    _tell_the_pharmacy(session, out, approved=False)
    return out


def _tell_the_pharmacy(session: Session, out: dict, *, approved: bool) -> None:
    """The pharmacist is standing at a counter waiting for this answer, and
    he asked from a screen he has since closed."""
    org = session.execute(
        text("SELECT organisation_id FROM pharmacy_account WHERE id=:p"),
        {"p": out["pharmacy_id"]}).scalar()
    emit(session,
         event_code="N-CREDIT-OVERRIDE-APPROVED" if approved else "N-CREDIT-OVERRIDE-DECLINED",
         recipient_user_id=out["requested_by_user_id"],
         recipient_org_id=org,
         payload={"credit_override_id": out["id"], "request_id": out["request_id"],
                  "vendor_id": out["vendor_id"], "vendor_name": out["vendor_name"],
                  "amount_requested": out["amount_requested"],
                  "decided_by_side": out["decided_by_side"],
                  "decision_reason": out["decision_reason"]})
