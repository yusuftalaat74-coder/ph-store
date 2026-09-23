"""A15.4 rows 114-134 (reduced set). Item-2 fix (backend-review-r1.md): the
order loop now closes end to end over HTTP — `accept` routes through SM-04
(never a raw `order_line` UPDATE), `pick` writes traceability, `dispatch`'s
SM-03 effect creates the `delivery_job` and its own traceability rows,
`delivery-jobs/*` drives SM-05 to a real `ATTEMPT_*`, and `receipt` writes
real `receipt_line` rows and takes the `RECEIPT_DISPUTE` branch (R-072). The
invented `mark-delivered` route is gone — delivery is only ever reached via
SM-05."""
from datetime import date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.money import money_str
from rova.domain.enums import RoleCode
from rova.domain.machines.registry import MACHINES

router = APIRouter(prefix="/v1", tags=["fulfilment"])

_VENDOR_DESK = (RoleCode.VENDOR_ORDER_DESK, RoleCode.VENDOR_ADMIN)
_VENDOR_PICKER = (RoleCode.VENDOR_PICKER, RoleCode.VENDOR_ORDER_DESK, RoleCode.VENDOR_ADMIN)
_PHARMACY = (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER, RoleCode.PHARMACY_RECEIVER)
_PHARMACY_DECIDERS = (RoleCode.PHARMACY_BUYER, RoleCode.PHARMACY_ADMIN, RoleCode.OPS_REVIEWER)
_RECEIVER = (RoleCode.PHARMACY_RECEIVER, RoleCode.PHARMACY_ADMIN)
_DISPATCHER_VENDOR_ADMIN = (RoleCode.DISPATCHER, RoleCode.VENDOR_ADMIN)
_COURIER = (RoleCode.COURIER,)
_ANY_ORDER_READER = _VENDOR_DESK + _PHARMACY + (RoleCode.OPS_REVIEWER, RoleCode.PLATFORM_ADMIN, RoleCode.COMPLIANCE_OFFICER)


def _order_or_404(session, order_id, principal):
    row = session.execute(text('SELECT * FROM "order" WHERE id=:o'), {"o": order_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "order not found")
    owns = (principal.pharmacy_id and principal.pharmacy_id == row["pharmacy_id"]) or \
           (principal.vendor_id and principal.vendor_id == row["vendor_id"]) or \
           principal.scope().kind == "platform"
    if not owns:
        raise ApiError("NOT_FOUND", "order not found")
    return row


def _order_line_or_404(session, line_id, principal):
    line = session.execute(text("SELECT * FROM order_line WHERE id=:id"), {"id": line_id}).mappings().first()
    if line is None:
        raise ApiError("NOT_FOUND", "order line not found")
    order = _order_or_404(session, line["order_id"], principal)
    return line, order


def _delivery_job_or_404(session, job_id, principal):
    job = session.execute(text("SELECT * FROM delivery_job WHERE id=:id"), {"id": job_id}).mappings().first()
    if job is None:
        raise ApiError("NOT_FOUND", "delivery job not found")
    owns = (principal.pharmacy_id and principal.pharmacy_id == job["pharmacy_id"]) or \
           (principal.vendor_id and principal.vendor_id == job["vendor_id"]) or \
           (principal.user_id and principal.user_id == job["courier_user_id"]) or \
           principal.scope().kind == "platform"
    if not owns:
        raise ApiError("NOT_FOUND", "delivery job not found")
    return job


def _order_with_lines(session, order_id: str) -> dict:
    row = session.execute(text('SELECT * FROM "order" WHERE id=:o'), {"o": order_id}).mappings().one()
    lines = session.execute(text("SELECT * FROM order_line WHERE order_id=:o"), {"o": order_id}).mappings().all()
    delivery_job = session.execute(
        text("SELECT * FROM delivery_job WHERE order_id=:o ORDER BY created_at DESC LIMIT 1"), {"o": order_id}
    ).mappings().first()
    fee_events = session.execute(
        text("SELECT fs.type, fs.payer, fev.amount FROM fee_event fev "
             "JOIN fee_schedule fs ON fs.id = fev.fee_schedule_id WHERE fev.order_id=:o"),
        {"o": order_id},
    ).mappings().all()
    # "Also fix" item: money_str() for consistency with checkout.py's
    # platform_fees.amount (both now 2dp, matching order.goods_total etc.),
    # not just because this particular column is already NUMERIC(14,2).
    platform_fees = [{"type": f["type"], "payer": f["payer"], "amount": money_str(f["amount"]),
                       "label_pt": "taxa de serviço"} for f in fee_events]
    return {**dict(row), "lines": [dict(l) for l in lines],
            "delivery_job": dict(delivery_job) if delivery_job else None, "platform_fees": platform_fees}


@router.get("/orders/{order_id}")
def get_order(order_id: str, principal: Principal = Depends(require_roles(*_ANY_ORDER_READER)),
              session: Session = Depends(get_session, scope="function")):
    _order_or_404(session, order_id, principal)
    return _order_with_lines(session, order_id)


class AcceptLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_line_id: str
    confirmed_qty: int


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines: list[AcceptLine]
    promised_dispatch_at: str | None = None


@router.post("/orders/{order_id}/accept")
def accept_order(order_id: str, body: AcceptBody, principal: Principal = Depends(require_roles(*_VENDOR_DESK)),
                  session: Session = Depends(get_session, scope="function")):
    """A8.5: routes every line through SM-04 (`CONFIRM_FULL`/`CONFIRM_SHORT`)
    before applying SM-03 `ACCEPT` — never a raw `order_line` write. All-zero
    confirmed quantities are R-052's REJECT semantics: the lines are left
    untouched (still `LINE_PENDING`) and the order is REJECTed outright."""
    order = _order_or_404(session, order_id, principal)
    if order["status"] != "PENDING_ACCEPTANCE":
        raise ApiError("GUARD_FAILED", "order is not PENDING_ACCEPTANCE")

    checked = []
    total_confirmed = 0
    for line in body.lines:
        ol = session.execute(text("SELECT ordered_qty FROM order_line WHERE id=:id AND order_id=:o"),
                              {"id": line.order_line_id, "o": order_id}).mappings().first()
        if ol is None:
            raise ApiError("NOT_FOUND", "order line not found")
        if line.confirmed_qty < 0 or line.confirmed_qty > ol["ordered_qty"]:
            raise ApiError("VALIDATION_ERROR", "confirmed_qty out of range")
        total_confirmed += line.confirmed_qty
        checked.append((line, ol))

    if total_confirmed > 0:
        for line, ol in checked:
            if line.confirmed_qty == ol["ordered_qty"]:
                MACHINES["SM-04"].apply(session, line.order_line_id, "CONFIRM_FULL", principal)
            else:
                MACHINES["SM-04"].apply(session, line.order_line_id, "CONFIRM_SHORT", principal,
                                         confirmed_qty=line.confirmed_qty)
        promised_dispatch_at = datetime.fromisoformat(body.promised_dispatch_at) if body.promised_dispatch_at else None
        updated = MACHINES["SM-03"].apply(session, order_id, "ACCEPT", principal,
                                           promised_dispatch_at=promised_dispatch_at)
    else:
        updated = MACHINES["SM-03"].apply(session, order_id, "REJECT", principal)
    return dict(updated)


@router.post("/orders/{order_id}/reject")
def reject_order(order_id: str, principal: Principal = Depends(require_roles(*_VENDOR_DESK)),
                  session: Session = Depends(get_session, scope="function")):
    _order_or_404(session, order_id, principal)
    updated = MACHINES["SM-03"].apply(session, order_id, "REJECT", principal)
    return dict(updated)


class ComplianceHaltBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


@router.post("/orders/{order_id}/compliance-halt")
def compliance_halt(order_id: str, body: ComplianceHaltBody,
                     principal: Principal = Depends(require_roles(RoleCode.COMPLIANCE_OFFICER)),
                     session: Session = Depends(get_session, scope="function")):
    """§6 ordering: the SM-04 line effects must run *before* the order's own
    `CANCELLED` write — the FSM engine always writes its subject row before
    running that transition's effects (`fsm.py Machine.apply`), so the only
    place that can put the `order_line` writes first is here, before SM-03's
    `apply()` is even called (see sm03_order.py::_effect_compliance_halt's
    docstring). The reason is validated up front against the same set the
    SM-03 guard re-checks, so an invalid reason 400s before any line is
    touched."""
    from rova.domain.machines.sm03_order import COMPLIANCE_HALT_REASONS
    _order_or_404(session, order_id, principal)
    if body.reason not in COMPLIANCE_HALT_REASONS:
        raise ApiError("GUARD_FAILED", f"reason must be one of {COMPLIANCE_HALT_REASONS}", rule="R-121")
    lines = session.execute(
        text("SELECT id FROM order_line WHERE order_id=:o AND fulfilment_status='LINE_FULL'"), {"o": order_id}
    ).mappings().all()
    for line in lines:
        MACHINES["SM-04"].apply(session, line["id"], "COMPLIANCE_HALT", "SYSTEM")
    updated = MACHINES["SM-03"].apply(session, order_id, "COMPLIANCE_HALT", principal, reason=body.reason)
    return dict(updated)


class PickBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_number: str
    lot_number: str
    expiry_date: date
    seal_ids: list[str]
    near_expiry_ack: bool = False


@router.post("/order-lines/{line_id}/pick")
def pick_order_line(line_id: str, body: PickBody,
                     principal: Principal = Depends(require_roles(*_VENDOR_PICKER)),
                     session: Session = Depends(get_session, scope="function")):
    """A8.6: writes batch/lot/expiry/seals on the line and one
    `traceability_event(PICKED)` per seal (R-059's near-expiry ack)."""
    line, order = _order_line_or_404(session, line_id, principal)
    if order["status"] != "ACCEPTED":
        raise ApiError("GUARD_FAILED", "order is not ACCEPTED")
    # SM-04's status here only tracks the *decision on the short remainder*
    # (REROUTE/DROP/ACCEPT_SUBSTITUTE all leave confirmed_qty untouched, per
    # sm04_order_line.py's own transitions) — whatever portion the vendor did
    # confirm (LINE_FULL, LINE_SHORT before a decision, or LINE_REROUTED /
    # LINE_DROPPED after one) still ships on *this* order and must still be
    # picked. Only LINE_PENDING (never confirmed) or an already-zeroed
    # confirmed_qty (licence-lapse/compliance-halt cascades) are excluded.
    if line["fulfilment_status"] == "LINE_PENDING" or line["confirmed_qty"] <= 0:
        raise ApiError("GUARD_FAILED", "line has no confirmed quantity to pick")
    if line["picked_at"] is not None:
        raise ApiError("GUARD_FAILED", "line already picked")
    if not body.seal_ids:
        raise ApiError("VALIDATION_ERROR", "at least one seal id is required")

    near_expiry_days = cfg.get(session, "CFG-NEAR-EXPIRY-DAYS", default=90)
    is_near_expiry = (body.expiry_date - date.today()).days < int(near_expiry_days)
    if is_near_expiry and not body.near_expiry_ack:
        raise ApiError("GUARD_FAILED", "near-expiry acknowledgement is required", rule="R-059")

    # Defect 6 fix: `uq_traceability_seal_dispatched` (a seal is dispatched
    # once, R-057) only fires when SM-03 DISPATCH later inserts
    # traceability_event(DISPATCHED) rows — by then it is a bare
    # IntegrityError with no ApiError handler for it, surfacing as 500
    # INTERNAL instead of a 4xx naming the duplicate seal. Check at pick
    # time instead, against both seals already DISPATCHED (the constraint
    # itself) and seals already sitting on another line's seal_ids in this
    # same order (a same-order duplicate would otherwise still 500 inside
    # that order's own single DISPATCH transaction, before either seal is
    # individually committed).
    dispatched_dupes = session.execute(
        text("SELECT DISTINCT seal_id FROM traceability_event WHERE event_type='DISPATCHED' "
             "AND seal_id = ANY(:seals)"),
        {"seals": body.seal_ids},
    ).scalars().all()
    picked_dupes = session.execute(
        text("SELECT DISTINCT unnest(seal_ids) AS seal_id FROM order_line "
             "WHERE order_id=:o AND id <> :line AND seal_ids && :seals"),
        {"o": order["id"], "line": line_id, "seals": body.seal_ids},
    ).scalars().all()
    dupes = sorted(set(dispatched_dupes) | set(picked_dupes))
    if dupes:
        raise ApiError(
            "VALIDATION_ERROR", f"seal id(s) already in use: {', '.join(dupes)}",
            details=[{"field": "seal_ids", "reason": s} for s in dupes], rule="R-057",
        )

    session.execute(
        text(
            "UPDATE order_line SET batch_number=:b, lot_number=:l, expiry_date=:e, seal_ids=:seals, "
            "near_expiry_ack_user_id=:ack, picked_at=:now, updated_at=:now WHERE id=:id"
        ),
        {"b": body.batch_number, "l": body.lot_number, "e": body.expiry_date, "seals": body.seal_ids,
         "ack": principal.user_id if is_near_expiry else None, "now": now(), "id": line_id},
    )
    actor_role = next(iter(principal.roles), "UNKNOWN")
    for seal in body.seal_ids:
        session.execute(
            text(
                "INSERT INTO traceability_event (id, order_line_id, seal_id, batch_number, lot_number, "
                "expiry_date, event_type, actor_user_id, actor_role) VALUES "
                "(:id, :ol, :seal, :b, :l, :e, 'PICKED', :uid, :role)"
            ),
            {"id": new_id("trc"), "ol": line_id, "seal": seal, "b": body.batch_number, "l": body.lot_number,
             "e": body.expiry_date, "uid": principal.user_id, "role": actor_role},
        )
    updated = session.execute(text("SELECT * FROM order_line WHERE id=:id"), {"id": line_id}).mappings().one()
    return dict(updated)


class RerouteDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str  # REROUTE | DROP | ACCEPT_SUBSTITUTE
    substitution_id: str | None = None


_REROUTE_TRIGGERS = {"REROUTE": "PHARMACY_REROUTE", "DROP": "DROP", "ACCEPT_SUBSTITUTE": "ACCEPT_SUBSTITUTE"}


@router.post("/order-lines/{line_id}/reroute-decision")
def reroute_decision(line_id: str, body: RerouteDecisionBody,
                      principal: Principal = Depends(require_roles(*_PHARMACY_DECIDERS)),
                      session: Session = Depends(get_session, scope="function")):
    line, order = _order_line_or_404(session, line_id, principal)
    trigger = _REROUTE_TRIGGERS.get(body.action)
    if trigger is None:
        raise ApiError("VALIDATION_ERROR", "action must be one of REROUTE, DROP, ACCEPT_SUBSTITUTE")
    kwargs = {}
    if body.substitution_id:
        kwargs["substitution_id"] = body.substitution_id
    updated = MACHINES["SM-04"].apply(session, line_id, trigger, principal, **kwargs)
    return dict(updated)


@router.post("/orders/{order_id}/dispatch")
def dispatch_order(order_id: str, principal: Principal = Depends(require_roles(*_VENDOR_PICKER)),
                    session: Session = Depends(get_session, scope="function")):
    """SM-03 DISPATCH: guard R-054, writes traceability_event(DISPATCHED) per
    seal and creates the delivery_job (both as the transition's own effects,
    same transaction) — see sm03_order.py::_effect_dispatch."""
    _order_or_404(session, order_id, principal)
    MACHINES["SM-03"].apply(session, order_id, "DISPATCH", principal)
    return _order_with_lines(session, order_id)


# ---------------------------------------------------------------------------
# Delivery jobs (SM-05)
# ---------------------------------------------------------------------------

class AssignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    courier_user_id: str
    transporter_id: str | None = None


@router.get("/delivery-jobs/{job_id}")
def get_delivery_job(job_id: str, principal: Principal = Depends(require_roles(
        *_DISPATCHER_VENDOR_ADMIN, *_COURIER, RoleCode.OPS_REVIEWER, RoleCode.PLATFORM_ADMIN)),
        session: Session = Depends(get_session, scope="function")):
    job = _delivery_job_or_404(session, job_id, principal)
    attempts = session.execute(
        text("SELECT * FROM delivery_attempt WHERE delivery_job_id=:id ORDER BY attempt_no"), {"id": job_id}
    ).mappings().all()
    return {**dict(job), "attempts": [dict(a) for a in attempts]}


@router.post("/delivery-jobs/{job_id}/assign")
def assign_delivery_job(job_id: str, body: AssignBody,
                         principal: Principal = Depends(require_roles(*_DISPATCHER_VENDOR_ADMIN)),
                         session: Session = Depends(get_session, scope="function")):
    _delivery_job_or_404(session, job_id, principal)
    updated = MACHINES["SM-05"].apply(session, job_id, "ASSIGN", principal,
                                       courier_user_id=body.courier_user_id, transporter_id=body.transporter_id)
    return dict(updated)


@router.post("/delivery-jobs/{job_id}/start")
def start_delivery_job(job_id: str, principal: Principal = Depends(require_roles(*_COURIER)),
                        session: Session = Depends(get_session, scope="function")):
    job = _delivery_job_or_404(session, job_id, principal)
    if job["courier_user_id"] != principal.user_id:
        raise ApiError("NOT_FOUND", "delivery job not found")
    updated = MACHINES["SM-05"].apply(session, job_id, "START", principal)
    return dict(updated)


class AttemptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: str  # DELIVERED | CLOSED | REFUSED | WRONG_ADDRESS | NO_AUTHORISED_RECEIVER
    proof_signature_or_code: str | None = None
    proof_photo_ref: str | None = None


@router.post("/delivery-jobs/{job_id}/attempts")
def record_delivery_attempt(job_id: str, body: AttemptBody,
                             principal: Principal = Depends(require_roles(*_COURIER)),
                             session: Session = Depends(get_session, scope="function")):
    job = session.execute(text("SELECT * FROM delivery_job WHERE id=:id FOR UPDATE"), {"id": job_id}).mappings().first()
    if job is None:
        raise ApiError("NOT_FOUND", "delivery job not found")
    if job["courier_user_id"] != principal.user_id:
        raise ApiError("NOT_FOUND", "delivery job not found")
    if body.outcome not in ("DELIVERED", "CLOSED", "REFUSED", "WRONG_ADDRESS", "NO_AUTHORISED_RECEIVER"):
        raise ApiError("VALIDATION_ERROR", "invalid outcome")

    attempt_no = job["attempt_count"] + 1
    is_delivered = body.outcome == "DELIVERED"
    if is_delivered and not (body.proof_signature_or_code or body.proof_photo_ref):
        raise ApiError("VALIDATION_ERROR", "DELIVERED requires proof_signature_or_code or proof_photo_ref",
                        details=[{"field": "proof", "reason": "R-069"}])

    session.execute(
        text(
            "INSERT INTO delivery_attempt (id, delivery_job_id, attempt_no, outcome, courier_user_id, "
            "proof_signature_or_code, proof_photo_ref, proof_captured_at) VALUES "
            "(:id, :job, :no, :outcome, :courier, :sig, :photo, :captured)"
        ),
        {"id": new_id("dla"), "job": job_id, "no": attempt_no, "outcome": body.outcome,
         "courier": principal.user_id, "sig": body.proof_signature_or_code, "photo": body.proof_photo_ref,
         "captured": now() if is_delivered else None},
    )

    if is_delivered:
        trigger = "ATTEMPT_DELIVERED"
    else:
        max_attempts = int(cfg.get(session, "CFG-DELIVERY-MAX-ATTEMPTS", default=3))
        trigger = "ATTEMPT_FAILED_RETRY" if attempt_no < max_attempts else "ATTEMPT_FAILED_FINAL"
    updated = MACHINES["SM-05"].apply(session, job_id, trigger, principal)
    return dict(updated)


@router.post("/delivery-jobs/{job_id}/reschedule")
def reschedule_delivery_job(job_id: str, principal: Principal = Depends(require_roles(RoleCode.DISPATCHER)),
                             session: Session = Depends(get_session, scope="function")):
    _delivery_job_or_404(session, job_id, principal)
    updated = MACHINES["SM-05"].apply(session, job_id, "RESCHEDULE", principal)
    return dict(updated)


# ---------------------------------------------------------------------------
# Receipt (SM-03 RECEIPT_ACCEPT / RECEIPT_DISPUTE)
# ---------------------------------------------------------------------------

class ReceiptLineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_line_id: str
    accepted_qty: int
    rejected_qty: int = 0
    reason_code: str | None = None


class ReceiptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines: list[ReceiptLineBody]
    channel_ref: str | None = None


@router.post("/orders/{order_id}/receipt")
def receipt(order_id: str, body: ReceiptBody, principal: Principal = Depends(require_roles(*_RECEIVER)),
            session: Session = Depends(get_session, scope="function")):
    """A8.6: writes `receipt`/`receipt_line`s (`accepted_qty + rejected_qty
    <= confirmed_qty` else 422), `traceability_event(RECEIVED)` per seal, and
    fires `RECEIPT_ACCEPT` or `RECEIPT_DISPUTE` (R-072) — the latter creates
    one `dispute(RECEIPT_DISCREPANCY)` per rejected line."""
    order = _order_or_404(session, order_id, principal)
    if order["status"] != "DELIVERED_PENDING_RECEIPT":
        raise ApiError("GUARD_FAILED", "order is not DELIVERED_PENDING_RECEIPT")

    receipt_id = new_id("rcp")
    session.execute(
        text("INSERT INTO receipt (id, order_id, received_by_user_id, channel_ref) VALUES (:id, :o, :uid, :ref)"),
        {"id": receipt_id, "o": order_id, "uid": principal.user_id, "ref": body.channel_ref},
    )

    actor_role = next(iter(principal.roles), "UNKNOWN")
    rejected_receipt_line_ids: list[str] = []
    for l in body.lines:
        line = session.execute(
            text("SELECT * FROM order_line WHERE id=:id AND order_id=:o"), {"id": l.order_line_id, "o": order_id}
        ).mappings().first()
        if line is None:
            raise ApiError("NOT_FOUND", "order line not found")
        if l.accepted_qty < 0 or l.rejected_qty < 0 or l.accepted_qty + l.rejected_qty > line["confirmed_qty"]:
            raise ApiError("VALIDATION_ERROR", "accepted_qty + rejected_qty must not exceed confirmed_qty")
        if l.rejected_qty > 0 and not l.reason_code:
            raise ApiError("VALIDATION_ERROR", "reason_code is required when rejected_qty > 0", rule="R-072")

        rline_id = new_id("rcl")
        session.execute(
            text(
                "INSERT INTO receipt_line (id, receipt_id, order_line_id, accepted_qty, rejected_qty, reason_code) "
                "VALUES (:id, :r, :ol, :acc, :rej, :reason)"
            ),
            {"id": rline_id, "r": receipt_id, "ol": l.order_line_id, "acc": l.accepted_qty,
             "rej": l.rejected_qty, "reason": l.reason_code},
        )
        if l.rejected_qty > 0:
            rejected_receipt_line_ids.append(rline_id)

        for seal in line["seal_ids"]:
            session.execute(
                text(
                    "INSERT INTO traceability_event (id, order_line_id, seal_id, batch_number, lot_number, "
                    "expiry_date, event_type, actor_user_id, actor_role) VALUES "
                    "(:id, :ol, :seal, :b, :l, :e, 'RECEIVED', :uid, :role)"
                ),
                {"id": new_id("trc"), "ol": l.order_line_id, "seal": seal, "b": line["batch_number"],
                 "l": line["lot_number"], "e": line["expiry_date"], "uid": principal.user_id, "role": actor_role},
            )

    if rejected_receipt_line_ids:
        updated = MACHINES["SM-03"].apply(session, order_id, "RECEIPT_DISPUTE", principal,
                                           receipt_line_ids=rejected_receipt_line_ids)
    else:
        updated = MACHINES["SM-03"].apply(session, order_id, "RECEIPT_ACCEPT", principal)
    return dict(updated)
