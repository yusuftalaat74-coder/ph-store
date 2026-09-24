"""Disputes, returns, credit notes and the day-to-day credit desk.

The arithmetic was already here and already adversarially tested: SM-09,
SM-10, `rova.credit.gate` and `rova.credit.exposure`. What was missing was a
surface for the people who actually work it — the vendor's finance desk, the
pharmacy raising an RMA, and the compliance officer who is the only role
allowed near a regulated-price incident.

Two invariants this module does not get to relax:

* `current_exposure` and `pending_exposure` are *derived*. They are never
  stored and never cached in a column here; every read recomputes them from
  `ledger_entry` and the live order book, which is why a credit screen can
  never drift from the ledger.
* `ledger_entry` is append-only at the database level. A correction is a new
  `ADJUSTMENT` row with the opposite sign, never an UPDATE — the trigger
  `rova_forbid_mutation()` refuses anything else, so this is enforced whether
  or not this module remembers to.
"""
import json
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.core.clock import now
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.money import money_str, quantize
from rova.credit import exposure as credit_exposure
from rova.credit.gate import check as credit_check
from rova.domain.enums import RoleCode
from rova.domain.machines.registry import MACHINES
from rova.integrations.office.router import require_not_office_managed  # PH Office SPEC 5.9.6
from rova.integrations.office import hooks as office_hooks

router = APIRouter(prefix="/v1", tags=["resolution"])

_PHARMACY = (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_RECEIVER, RoleCode.PHARMACY_BUYER)
_VENDOR_FINANCE = (RoleCode.VENDOR_FINANCE, RoleCode.VENDOR_ADMIN)
_COMPLIANCE = (RoleCode.COMPLIANCE_OFFICER,)
_PLATFORM_FINANCE = (RoleCode.PLATFORM_FINANCE, RoleCode.PLATFORM_ADMIN)
_DISPUTE_READERS = _PHARMACY + _VENDOR_FINANCE + _COMPLIANCE + (RoleCode.OPS_REVIEWER,)
_CREDIT_READERS = _VENDOR_FINANCE + _PLATFORM_FINANCE + (RoleCode.PHARMACY_ADMIN, RoleCode.OPS_REVIEWER)

DisputeType = Literal["RECEIPT_DISCREPANCY", "REGULATED_PRICE_INCIDENT", "FREE_PRICE_MISMATCH"]
DisputeOutcome = Literal["CREDIT_NOTE", "REPLACEMENT", "REJECTED", "ESCALATED_TO_COMPLIANCE"]
ReturnReason = Literal["DAMAGED", "EXPIRED", "NEAR_EXPIRY", "WRONG_ITEM", "OTHER"]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _row_or_404(session: Session, table: str, row_id: str, label: str) -> dict:
    row = session.execute(text(f'SELECT * FROM "{table}" WHERE id=:id'), {"id": row_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", f"{label} not found")
    return dict(row)


def _order_in_scope(session: Session, order_id: str, principal: Principal) -> dict:
    order = _row_or_404(session, "order", order_id, "order")
    if principal.pharmacy_id and order["pharmacy_id"] != principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "order not found")
    if principal.vendor_id and order["vendor_id"] != principal.vendor_id:
        raise ApiError("NOT_FOUND", "order not found")
    return order


def _dispute_in_scope(session: Session, dispute_id: str, principal: Principal) -> tuple[dict, dict]:
    dispute = _row_or_404(session, "dispute", dispute_id, "dispute")
    order = _order_in_scope(session, dispute["order_id"], principal)
    return dispute, order


def _audit(session: Session, principal: Principal, *, action: str, subject_type: str, subject_id: str,
           rule_ref: str | None = None, metadata: dict | None = None) -> None:
    session.execute(
        text("INSERT INTO audit_event (id, actor_user_id, actor_role, action_code, subject_type, subject_id, "
             "rule_ref, metadata, occurred_at) VALUES (:id, :u, :ar, :a, :st, :si, :rr, CAST(:m AS JSONB), :t)"),
        {"id": new_id("aud"), "u": principal.user_id,
         "ar": next(iter(sorted(principal.roles)), "UNKNOWN"), "a": action, "st": subject_type,
         "si": subject_id, "rr": rule_ref, "m": json.dumps(metadata or {}), "t": now()},
    )


# ------------------------------------------------------------------ disputes

class OpenDispute(Body):
    order_id: str
    type: DisputeType
    receipt_line_id: str | None = None
    invoice_line_id: str | None = None
    notes: str | None = Field(default=None, max_length=4000)


@router.post("/disputes", status_code=201)
def open_dispute(body: OpenDispute,
                 principal: Principal = Depends(require_roles(*(_PHARMACY + _COMPLIANCE))),
                 session: Session = Depends(get_session, scope="function")):
    """A dispute is always *about* exactly one thing: a receipt line or an
    invoice line, never both and never neither. `ck_dispute_subject` enforces
    that with an XOR in the database; this check exists only so the caller is
    told which field is wrong instead of receiving a constraint violation."""
    if bool(body.receipt_line_id) == bool(body.invoice_line_id):
        raise ApiError("VALIDATION_ERROR", "exactly one of receipt_line_id or invoice_line_id is required",
                       details=[{"field": "receipt_line_id", "reason": "exactly one subject must be given"}])
    _order_in_scope(session, body.order_id, principal)

    if body.receipt_line_id:
        owner = session.execute(
            text("SELECT r.order_id FROM receipt_line rl JOIN receipt r ON r.id = rl.receipt_id WHERE rl.id=:id"),
            {"id": body.receipt_line_id},
        ).scalar()
        if owner != body.order_id:
            raise ApiError("VALIDATION_ERROR", "receipt_line_id does not belong to this order")
    else:
        owner = session.execute(
            text("SELECT i.order_id FROM invoice_line il JOIN invoice i ON i.id = il.invoice_id WHERE il.id=:id"),
            {"id": body.invoice_line_id},
        ).scalar()
        if owner != body.order_id:
            raise ApiError("VALIDATION_ERROR", "invoice_line_id does not belong to this order")

    raised_by = "ComplianceOfficer" if RoleCode.COMPLIANCE_OFFICER in principal.roles else (
        "PharmacyReceiver" if RoleCode.PHARMACY_RECEIVER in principal.roles else "PharmacyAdmin")
    did = new_id("dsp")
    session.execute(
        text("INSERT INTO dispute (id, order_id, receipt_line_id, invoice_line_id, type, raised_by, status, notes) "
             "VALUES (:id, :o, :rl, :il, :ty, :rb, 'OPEN', :n)"),
        {"id": did, "o": body.order_id, "rl": body.receipt_line_id, "il": body.invoice_line_id,
         "ty": body.type, "rb": raised_by, "n": body.notes},
    )
    return _row_or_404(session, "dispute", did, "dispute")


@router.get("/disputes")
def list_disputes(status: str | None = None, type: DisputeType | None = None,
                  order_id: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                  principal: Principal = Depends(require_roles(*_DISPUTE_READERS)),
                  session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text('SELECT d.* FROM dispute d JOIN "order" o ON o.id = d.order_id '
             "WHERE (CAST(:ph AS TEXT) IS NULL OR o.pharmacy_id=:ph) "
             "AND (CAST(:vn AS TEXT) IS NULL OR o.vendor_id=:vn) "
             "AND (CAST(:st AS TEXT) IS NULL OR d.status=:st) "
             "AND (CAST(:ty AS TEXT) IS NULL OR d.type=:ty) "
             "AND (CAST(:oi AS TEXT) IS NULL OR d.order_id=:oi) "
             "ORDER BY d.created_at DESC LIMIT :lim"),
        {"ph": principal.pharmacy_id, "vn": principal.vendor_id, "st": status, "ty": type,
         "oi": order_id, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/disputes/{dispute_id}")
def get_dispute(dispute_id: str,
                principal: Principal = Depends(require_roles(*_DISPUTE_READERS)),
                session: Session = Depends(get_session, scope="function")):
    dispute, order = _dispute_in_scope(session, dispute_id, principal)
    transitions = session.execute(
        text("SELECT * FROM state_transition WHERE subject_type='dispute' AND subject_id=:d "
             "ORDER BY occurred_at ASC"), {"d": dispute_id},
    ).mappings().all()
    return {**dispute, "order_number": order["number"], "transitions": [dict(t) for t in transitions]}


@router.post("/disputes/{dispute_id}/assign")
def assign_dispute(dispute_id: str,
                   principal: Principal = Depends(require_roles(*(_VENDOR_FINANCE + _COMPLIANCE))),
                   session: Session = Depends(get_session, scope="function")):
    _dispute_in_scope(session, dispute_id, principal)
    return dict(MACHINES["SM-09"].apply(session, dispute_id, "ASSIGN", principal))


@router.post("/disputes/{dispute_id}/escalate")
def escalate_dispute(dispute_id: str,
                     principal: Principal = Depends(require_roles(*(_VENDOR_FINANCE + _COMPLIANCE))),
                     session: Session = Depends(get_session, scope="function")):
    _dispute_in_scope(session, dispute_id, principal)
    return dict(MACHINES["SM-09"].apply(session, dispute_id, "ESCALATE", principal))


class ResolveDispute(Body):
    outcome: DisputeOutcome
    notes: str | None = Field(default=None, max_length=4000)


@router.post("/disputes/{dispute_id}/resolve")
def resolve_dispute(dispute_id: str, body: ResolveDispute,
                    principal: Principal = Depends(require_roles(*(_VENDOR_FINANCE + _COMPLIANCE))),
                    session: Session = Depends(get_session, scope="function")):
    """R-074: a `REGULATED_PRICE_INCIDENT` can only be resolved by a
    ComplianceOfficer — enforced inside SM-09's own guard, not re-checked
    here, so the rule has exactly one home."""
    dispute, _ = _dispute_in_scope(session, dispute_id, principal)
    trigger = "RESOLVE_ESCALATED" if dispute["status"] == "ESCALATED" else "RESOLVE"
    result = MACHINES["SM-09"].apply(session, dispute_id, trigger, principal,
                                     outcome=body.outcome, notes=body.notes)
    if dispute["type"] == "REGULATED_PRICE_INCIDENT":
        _audit(session, principal, action="PRICE_DEVIATION_RESOLUTION", rule_ref="R-074",
               subject_type="dispute", subject_id=dispute_id, metadata={"outcome": body.outcome})
    return dict(result)


# ------------------------------------------------------------------ returns

class ReturnLineIn(Body):
    order_line_id: str
    qty: int = Field(gt=0)
    reason_code: ReturnReason


class OpenReturn(Body):
    order_id: str
    origin: Literal["PHARMACY_RMA", "DISPUTE_RESOLUTION", "DELIVERY_EXHAUSTED"] = "PHARMACY_RMA"
    lines: list[ReturnLineIn] = Field(min_length=1)


@router.post("/returns", status_code=201)
def open_return(body: OpenReturn,
                principal: Principal = Depends(require_roles(RoleCode.PHARMACY_ADMIN, *_COMPLIANCE)),
                session: Session = Depends(get_session, scope="function")):
    """An RMA can never return more than was accepted. The quantity accepted
    is read from the receipt, not from the order — a line that was short
    delivered cannot be returned at its ordered quantity."""
    order = _order_in_scope(session, body.order_id, principal)
    rid = new_id("ret")
    seq = session.execute(text("SELECT count(*) FROM return WHERE order_id=:o"), {"o": body.order_id}).scalar()
    rma = f"RMA-{order['number']}-{str(seq + 1).zfill(2)}"

    for i, line in enumerate(body.lines):
        owner = session.execute(text("SELECT order_id FROM order_line WHERE id=:id"),
                                {"id": line.order_line_id}).scalar()
        if owner != body.order_id:
            raise ApiError("VALIDATION_ERROR", "order_line_id does not belong to this order",
                           details=[{"field": f"lines.{i}.order_line_id", "reason": "wrong order"}])
        accepted = session.execute(
            text("SELECT COALESCE(SUM(accepted_qty), 0) FROM receipt_line WHERE order_line_id=:id"),
            {"id": line.order_line_id},
        ).scalar()
        already = session.execute(
            text("SELECT COALESCE(SUM(rl.qty), 0) FROM return_line rl JOIN return r ON r.id = rl.return_id "
                 "WHERE rl.order_line_id=:id AND r.status <> 'REJECTED'"),
            {"id": line.order_line_id},
        ).scalar()
        if line.qty + already > accepted:
            raise ApiError("GUARD_FAILED",
                           f"cannot return {line.qty}: {accepted} accepted, {already} already in a return",
                           details=[{"field": f"lines.{i}.qty", "reason": "exceeds the accepted quantity"}])

    session.execute(
        text("INSERT INTO return (id, order_id, rma_number, status, requested_by_user_id, origin) "
             "VALUES (:id, :o, :rma, 'REQUESTED', :u, :og)"),
        {"id": rid, "o": body.order_id, "rma": rma, "u": principal.user_id, "og": body.origin},
    )
    for line in body.lines:
        session.execute(
            text("INSERT INTO return_line (id, return_id, order_line_id, qty, reason_code) "
                 "VALUES (:id, :r, :ol, :q, :rc)"),
            {"id": new_id("rtl"), "r": rid, "ol": line.order_line_id, "q": line.qty, "rc": line.reason_code},
        )
    return _return_with_lines(session, rid)


def _return_with_lines(session: Session, return_id: str) -> dict:
    row = _row_or_404(session, "return", return_id, "return")
    lines = session.execute(text("SELECT * FROM return_line WHERE return_id=:r"),
                            {"r": return_id}).mappings().all()
    return {**row, "lines": [dict(l) for l in lines]}


@router.get("/returns")
def list_returns(status: str | None = None, order_id: str | None = None,
                 limit: int = Query(default=50, ge=1, le=200),
                 principal: Principal = Depends(require_roles(*_DISPUTE_READERS)),
                 session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text('SELECT rt.* FROM return rt JOIN "order" o ON o.id = rt.order_id '
             "WHERE (CAST(:ph AS TEXT) IS NULL OR o.pharmacy_id=:ph) "
             "AND (CAST(:vn AS TEXT) IS NULL OR o.vendor_id=:vn) "
             "AND (CAST(:st AS TEXT) IS NULL OR rt.status=:st) "
             "AND (CAST(:oi AS TEXT) IS NULL OR rt.order_id=:oi) "
             "ORDER BY rt.created_at DESC LIMIT :lim"),
        {"ph": principal.pharmacy_id, "vn": principal.vendor_id, "st": status, "oi": order_id, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/returns/{return_id}")
def get_return(return_id: str,
               principal: Principal = Depends(require_roles(*_DISPUTE_READERS)),
               session: Session = Depends(get_session, scope="function")):
    row = _row_or_404(session, "return", return_id, "return")
    _order_in_scope(session, row["order_id"], principal)
    return _return_with_lines(session, return_id)


def _return_transition(trigger: str, roles):
    def _endpoint(return_id: str,
                  principal: Principal = Depends(require_roles(*roles)),
                  session: Session = Depends(get_session, scope="function")):
        row = _row_or_404(session, "return", return_id, "return")
        _order_in_scope(session, row["order_id"], principal)
        MACHINES["SM-10"].apply(session, return_id, trigger, principal)
        return _return_with_lines(session, return_id)
    return _endpoint


# PH Office SPEC 1.3 (v1.1): the return lifecycle (approve / reject / ship /
# receive) stays in PH Store in the MVP — PH Office only books the credit note
# (SPEC 2.3, BO-SM-10 deferred). These routes are therefore NOT locked when the
# link is on; Store emits return.approved / return.rejected / return.received.
router.add_api_route("/returns/{return_id}/approve", _return_transition("APPROVE", _VENDOR_FINANCE),
                     methods=["POST"], name="approve_return", tags=["resolution"])
router.add_api_route("/returns/{return_id}/reject", _return_transition("REJECT", _VENDOR_FINANCE),
                     methods=["POST"], name="reject_return", tags=["resolution"])
router.add_api_route("/returns/{return_id}/ship", _return_transition("SHIP", (RoleCode.DISPATCHER,)),
                     methods=["POST"], name="ship_return", tags=["resolution"])
router.add_api_route("/returns/{return_id}/receive", _return_transition("RECEIVE", _VENDOR_FINANCE),
                     methods=["POST"], name="receive_return", tags=["resolution"])


# ------------------------------------------------------------------ credit notes

class IssueCreditNote(Body):
    order_id: str
    amount: Decimal = Field(gt=0)
    return_id: str | None = None
    dispute_id: str | None = None


@router.post("/credit-notes", status_code=201, dependencies=[Depends(require_not_office_managed)])
def issue_credit_note(body: IssueCreditNote,
                      principal: Principal = Depends(require_roles(*_VENDOR_FINANCE)),
                      session: Session = Depends(get_session, scope="function")):
    """Issuing a credit note writes one `CREDIT_NOTE` ledger entry with a
    negative amount against the facility, which is what makes the pharmacy's
    exposure fall. The ledger row and the credit note are written in the same
    transaction: a credit note that does not move the ledger is not a credit
    note, it is a promise."""
    order = _order_in_scope(session, body.order_id, principal)
    amount = quantize(body.amount)

    # `ck_credit_note_origin` — a credit note always answers something: an
    # approved return or a resolved dispute. Money does not go back to a
    # pharmacy because someone felt like it, and the database will not store
    # a note that names neither. Checked here so the caller is told which
    # field is missing instead of being handed a constraint violation.
    if not body.return_id and not body.dispute_id:
        raise ApiError("VALIDATION_ERROR", "a credit note must cite a return or a dispute",
                       details=[{"field": "return_id",
                                 "reason": "one of return_id or dispute_id is required"}])

    if body.return_id:
        ret = _row_or_404(session, "return", body.return_id, "return")
        if ret["order_id"] != body.order_id:
            raise ApiError("VALIDATION_ERROR", "return_id does not belong to this order")
    if body.dispute_id:
        dsp = _row_or_404(session, "dispute", body.dispute_id, "dispute")
        if dsp["order_id"] != body.order_id:
            raise ApiError("VALIDATION_ERROR", "dispute_id does not belong to this order")

    invoiced = session.execute(
        text("SELECT COALESCE(SUM(total_amount), 0) FROM invoice WHERE order_id=:o AND status <> 'WRITTEN_OFF'"),
        {"o": body.order_id},
    ).scalar()
    credited = session.execute(
        text("SELECT COALESCE(SUM(amount), 0) FROM credit_note WHERE order_id=:o"), {"o": body.order_id},
    ).scalar()
    if amount + Decimal(credited) > Decimal(invoiced):
        raise ApiError("GUARD_FAILED",
                       f"credit notes for this order would exceed what was invoiced "
                       f"({money_str(Decimal(invoiced))})")

    cid = new_id("crn")
    session.execute(
        text("INSERT INTO credit_note (id, vendor_id, pharmacy_id, order_id, return_id, dispute_id, amount, "
             "issued_at) VALUES (:id, :v, :p, :o, :r, :d, :a, :t)"),
        {"id": cid, "v": order["vendor_id"], "p": order["pharmacy_id"], "o": body.order_id,
         "r": body.return_id, "d": body.dispute_id, "a": amount, "t": now()},
    )

    facility = session.execute(
        text("SELECT id FROM credit_facility WHERE vendor_id=:v AND pharmacy_id=:p"),
        {"v": order["vendor_id"], "p": order["pharmacy_id"]},
    ).scalar()
    if facility:
        session.execute(
            text("INSERT INTO ledger_entry (id, credit_facility_id, entry_type, reference_type, reference_id, "
                 "amount) VALUES (:id, :f, 'CREDIT_NOTE', 'credit_note', :ref, :a)"),
            {"id": new_id("led"), "f": facility, "ref": cid, "a": -amount},
        )
    return {**_row_or_404(session, "credit_note", cid, "credit note"),
            "ledger_posted": facility is not None}


@router.get("/credit-notes")
def list_credit_notes(order_id: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                      principal: Principal = Depends(require_roles(*_CREDIT_READERS)),
                      session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM credit_note WHERE (CAST(:ph AS TEXT) IS NULL OR pharmacy_id=:ph) "
             "AND (CAST(:vn AS TEXT) IS NULL OR vendor_id=:vn) "
             "AND (CAST(:oi AS TEXT) IS NULL OR order_id=:oi) ORDER BY issued_at DESC LIMIT :lim"),
        {"ph": principal.pharmacy_id, "vn": principal.vendor_id, "oi": order_id, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


# ------------------------------------------------------------------ credit desk

class CreateFacility(Body):
    vendor_id: str
    pharmacy_id: str
    limit_amount: Decimal = Field(ge=0)
    opening_balance: Decimal = Decimal("0")
    terms_days: int | None = Field(default=None, gt=0)
    mov_waived: bool = False


@router.post("/credit-facilities", status_code=201)
def create_facility(body: CreateFacility,
                    principal: Principal = Depends(require_roles(*_VENDOR_FINANCE)),
                    session: Session = Depends(get_session, scope="function")):
    """A non-zero opening balance is a claim about money owed before the
    platform existed. `ck_credit_opening_attested` will not let it be stored
    without an attestation timestamp, so the attestation is recorded here at
    the moment it is claimed and by whom."""
    _row_or_404(session, "vendor_account", body.vendor_id, "vendor")
    _row_or_404(session, "pharmacy_account", body.pharmacy_id, "pharmacy")
    if principal.vendor_id and principal.vendor_id != body.vendor_id:
        raise ApiError("FORBIDDEN", "cannot open a facility for another vendor")
    existing = session.execute(
        text("SELECT id FROM credit_facility WHERE vendor_id=:v AND pharmacy_id=:p"),
        {"v": body.vendor_id, "p": body.pharmacy_id},
    ).scalar()
    if existing:
        raise ApiError("CONFLICT", "a facility already exists for this vendor and pharmacy")

    opening = quantize(body.opening_balance)
    fid = new_id("crf")
    session.execute(
        text("INSERT INTO credit_facility (id, vendor_id, pharmacy_id, limit_amount, opening_balance, "
             "opening_balance_attested_at, terms_days, mov_waived, status) "
             "VALUES (:id, :v, :p, :l, :ob, :at, :td, :mw, 'ACTIVE')"),
        {"id": fid, "v": body.vendor_id, "p": body.pharmacy_id, "l": quantize(body.limit_amount),
         "ob": opening, "at": now() if opening != 0 else None,
         "td": body.terms_days, "mw": body.mov_waived},
    )
    if opening != 0:
        _audit(session, principal, action="CREDIT_LIMIT_OVERRIDE", subject_type="credit_facility",
               subject_id=fid, metadata={"opening_balance": money_str(opening)})
    office_hooks.credit_facility_created(session, fid)   # PH Office link: no-op when disabled
    return _facility_view(session, fid)


def _facility_view(session: Session, facility_id: str) -> dict:
    row = _row_or_404(session, "credit_facility", facility_id, "credit facility")
    current = credit_exposure.current_exposure(session, facility_id)
    pending = credit_exposure.pending_exposure(session, row["vendor_id"], row["pharmacy_id"])
    return {
        **row,
        # derived, never stored — recomputed on every read from the ledger
        # and the live order book (A3.3)
        "current_exposure": money_str(current),
        "pending_exposure": money_str(pending),
        "headroom": money_str(Decimal(row["limit_amount"]) - current - pending),
    }


@router.get("/credit-facilities")
def list_facilities(vendor_id: str | None = None, pharmacy_id: str | None = None,
                    status: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                    principal: Principal = Depends(require_roles(*_CREDIT_READERS)),
                    session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT id FROM credit_facility WHERE "
             "(CAST(:pv AS TEXT) IS NULL OR vendor_id=:pv) AND (CAST(:pp AS TEXT) IS NULL OR pharmacy_id=:pp) "
             "AND (CAST(:v AS TEXT) IS NULL OR vendor_id=:v) AND (CAST(:p AS TEXT) IS NULL OR pharmacy_id=:p) "
             "AND (CAST(:st AS TEXT) IS NULL OR status=:st) ORDER BY created_at DESC LIMIT :lim"),
        {"pv": principal.vendor_id, "pp": principal.pharmacy_id, "v": vendor_id, "p": pharmacy_id,
         "st": status, "lim": limit},
    ).scalars().all()
    return {"items": [_facility_view(session, f) for f in rows], "next_cursor": None}


@router.get("/credit-facilities/{facility_id}")
def get_facility(facility_id: str,
                 principal: Principal = Depends(require_roles(*_CREDIT_READERS)),
                 session: Session = Depends(get_session, scope="function")):
    row = _row_or_404(session, "credit_facility", facility_id, "credit facility")
    if principal.vendor_id and row["vendor_id"] != principal.vendor_id:
        raise ApiError("NOT_FOUND", "credit facility not found")
    if principal.pharmacy_id and row["pharmacy_id"] != principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "credit facility not found")
    return _facility_view(session, facility_id)


class PatchFacility(Body):
    limit_amount: Decimal | None = Field(default=None, ge=0)
    terms_days: int | None = Field(default=None, gt=0)
    mov_waived: bool | None = None
    reason: str = Field(min_length=1, max_length=500)


@router.patch("/credit-facilities/{facility_id}", dependencies=[Depends(require_not_office_managed)])
def patch_facility(facility_id: str, body: PatchFacility,
                   principal: Principal = Depends(require_roles(*_VENDOR_FINANCE)),
                   session: Session = Depends(get_session, scope="function")):
    """Changing a credit limit is always audited with the reason given. A
    limit change with no reason is not accepted — `reason` is required, not
    optional, because this is the field a dispute six months from now turns
    on."""
    row = _row_or_404(session, "credit_facility", facility_id, "credit facility")
    if principal.vendor_id and row["vendor_id"] != principal.vendor_id:
        raise ApiError("NOT_FOUND", "credit facility not found")
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k != "reason"}
    if not fields:
        raise ApiError("VALIDATION_ERROR", "no fields to update")
    if "limit_amount" in fields:
        fields["limit_amount"] = quantize(Decimal(fields["limit_amount"]))
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    session.execute(text(f"UPDATE credit_facility SET {sets}, updated_at = :t WHERE id = :id"),
                    {**fields, "t": now(), "id": facility_id})
    _audit(session, principal, action="CREDIT_LIMIT_OVERRIDE", subject_type="credit_facility",
           subject_id=facility_id,
           metadata={"reason": body.reason,
                     "before": {k: str(row[k]) for k in fields},
                     "after": {k: str(v) for k, v in fields.items()}})
    return _facility_view(session, facility_id)


@router.post("/credit-facilities/{facility_id}/suspend", dependencies=[Depends(require_not_office_managed)])
def suspend_facility(facility_id: str, body: PatchFacility | None = None,
                     principal: Principal = Depends(require_roles(*_VENDOR_FINANCE)),
                     session: Session = Depends(get_session, scope="function")):
    row = _row_or_404(session, "credit_facility", facility_id, "credit facility")
    if principal.vendor_id and row["vendor_id"] != principal.vendor_id:
        raise ApiError("NOT_FOUND", "credit facility not found")
    if row["status"] == "SUSPENDED":
        raise ApiError("ILLEGAL_TRANSITION", "facility is already SUSPENDED")
    session.execute(text("UPDATE credit_facility SET status='SUSPENDED', updated_at=:t WHERE id=:id"),
                    {"t": now(), "id": facility_id})
    _audit(session, principal, action="CREDIT_LIMIT_OVERRIDE", subject_type="credit_facility",
           subject_id=facility_id, metadata={"status": "SUSPENDED"})
    return _facility_view(session, facility_id)


@router.post("/credit-facilities/{facility_id}/reinstate", dependencies=[Depends(require_not_office_managed)])
def reinstate_facility(facility_id: str,
                       principal: Principal = Depends(require_roles(*_VENDOR_FINANCE)),
                       session: Session = Depends(get_session, scope="function")):
    row = _row_or_404(session, "credit_facility", facility_id, "credit facility")
    if principal.vendor_id and row["vendor_id"] != principal.vendor_id:
        raise ApiError("NOT_FOUND", "credit facility not found")
    if row["status"] != "SUSPENDED":
        raise ApiError("ILLEGAL_TRANSITION", "facility is not SUSPENDED")
    session.execute(text("UPDATE credit_facility SET status='ACTIVE', updated_at=:t WHERE id=:id"),
                    {"t": now(), "id": facility_id})
    _audit(session, principal, action="CREDIT_LIMIT_OVERRIDE", subject_type="credit_facility",
           subject_id=facility_id, metadata={"status": "ACTIVE"})
    return _facility_view(session, facility_id)


@router.get("/credit-facilities/{facility_id}/ledger")
def facility_ledger(facility_id: str, limit: int = Query(default=200, ge=1, le=1000),
                    principal: Principal = Depends(require_roles(*_CREDIT_READERS)),
                    session: Session = Depends(get_session, scope="function")):
    """The append-only ledger behind the exposure number, with a running
    balance computed here rather than stored anywhere."""
    row = _row_or_404(session, "credit_facility", facility_id, "credit facility")
    if principal.vendor_id and row["vendor_id"] != principal.vendor_id:
        raise ApiError("NOT_FOUND", "credit facility not found")
    if principal.pharmacy_id and row["pharmacy_id"] != principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "credit facility not found")
    entries = session.execute(
        text("SELECT * FROM ledger_entry WHERE credit_facility_id=:f ORDER BY created_at ASC, id ASC LIMIT :lim"),
        {"f": facility_id, "lim": limit},
    ).mappings().all()
    running = Decimal(row["opening_balance"])
    out = []
    for e in entries:
        running += Decimal(e["amount"])
        out.append({**dict(e), "amount": money_str(Decimal(e["amount"])),
                    "running_balance": money_str(running)})
    return {"facility_id": facility_id,
            "opening_balance": money_str(Decimal(row["opening_balance"])),
            "items": out, "closing_balance": money_str(running)}


class GateCheck(Body):
    order_value: Decimal = Field(gt=0)


@router.post("/credit-facilities/{facility_id}/gate-check")
def gate_check(facility_id: str, body: GateCheck,
               principal: Principal = Depends(require_roles(*_CREDIT_READERS)),
               session: Session = Depends(get_session, scope="function")):
    """What the credit gate would decide for an order of this size right now,
    without placing one. The same `rova.credit.gate.check` the checkout path
    calls — not a second copy of the arithmetic."""
    row = _row_or_404(session, "credit_facility", facility_id, "credit facility")
    if principal.vendor_id and row["vendor_id"] != principal.vendor_id:
        raise ApiError("NOT_FOUND", "credit facility not found")
    if principal.pharmacy_id and row["pharmacy_id"] != principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "credit facility not found")
    result = credit_check(session, facility_id, row["vendor_id"], row["pharmacy_id"],
                          quantize(body.order_value))
    return {"blocked": result.blocked,
            "current_exposure": money_str(result.current_exposure),
            "pending_exposure": money_str(result.pending_exposure),
            "headroom": money_str(result.headroom),
            "options": result.options}


@router.get("/credit-facilities/{facility_id}/statement")
def facility_statement(facility_id: str,
                       principal: Principal = Depends(require_roles(*_CREDIT_READERS)),
                       session: Session = Depends(get_session, scope="function")):
    """The invoice-level view a finance desk chases payment from: what is
    open, what is overdue, and by how long."""
    row = _row_or_404(session, "credit_facility", facility_id, "credit facility")
    if principal.vendor_id and row["vendor_id"] != principal.vendor_id:
        raise ApiError("NOT_FOUND", "credit facility not found")
    if principal.pharmacy_id and row["pharmacy_id"] != principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "credit facility not found")
    invoices = session.execute(
        text("SELECT i.id, i.vendor_invoice_number, i.total_amount, i.status, i.finalised_at, "
             "COALESCE((SELECT SUM(pa.amount) FROM payment_allocation pa WHERE pa.invoice_id = i.id), 0) AS paid "
             "FROM invoice i WHERE i.issuer_vendor_id=:v AND i.pharmacy_id=:p "
             "AND i.status IN ('AWAITING_PAYMENT','PARTIALLY_PAID') ORDER BY i.finalised_at ASC NULLS LAST"),
        {"v": row["vendor_id"], "p": row["pharmacy_id"]},
    ).mappings().all()
    terms_days = row["terms_days"]
    today = now()
    items, open_total, overdue_total = [], Decimal("0"), Decimal("0")
    for inv in invoices:
        outstanding = Decimal(inv["total_amount"]) - Decimal(inv["paid"])
        days_open = (today - inv["finalised_at"]).days if inv["finalised_at"] else None
        overdue = bool(terms_days and days_open is not None and days_open > terms_days)
        open_total += outstanding
        if overdue:
            overdue_total += outstanding
        items.append({"invoice_id": inv["id"], "vendor_invoice_number": inv["vendor_invoice_number"],
                      "total_amount": money_str(Decimal(inv["total_amount"])),
                      "paid": money_str(Decimal(inv["paid"])),
                      "outstanding": money_str(outstanding), "status": inv["status"],
                      "days_open": days_open, "overdue": overdue})
    return {"facility_id": facility_id, "terms_days": terms_days,
            "open_total": money_str(open_total), "overdue_total": money_str(overdue_total),
            "items": items}


class Adjustment(Body):
    amount: Decimal
    reason: str = Field(min_length=1, max_length=500)


@router.post("/credit-facilities/{facility_id}/adjustments", status_code=201, dependencies=[Depends(require_not_office_managed)])
def post_adjustment(facility_id: str, body: Adjustment,
                    principal: Principal = Depends(require_roles(*_PLATFORM_FINANCE)),
                    session: Session = Depends(get_session, scope="function")):
    """The only way to correct a ledger. `ledger_entry` is append-only at the
    database level (`rova_forbid_mutation()`), so a correction is a new signed
    `ADJUSTMENT` row, never an edit of the row that was wrong. Both rows stay
    visible in the statement — which is the point."""
    _row_or_404(session, "credit_facility", facility_id, "credit facility")
    amount = quantize(body.amount)
    if amount == 0:
        raise ApiError("VALIDATION_ERROR", "an adjustment of zero has no effect")
    entry_id = new_id("led")
    session.execute(
        text("INSERT INTO ledger_entry (id, credit_facility_id, entry_type, reference_type, reference_id, amount) "
             "VALUES (:id, :f, 'ADJUSTMENT', 'invoice_write_off', :ref, :a)"),
        {"id": entry_id, "f": facility_id, "ref": entry_id, "a": amount},
    )
    _audit(session, principal, action="CREDIT_LIMIT_OVERRIDE", subject_type="ledger_entry",
           subject_id=entry_id, metadata={"reason": body.reason, "amount": money_str(amount)})
    return {"id": entry_id, "amount": money_str(amount), "reason": body.reason,
            **{"facility": _facility_view(session, facility_id)}}


@router.get("/credit-exposure/summary")
def exposure_summary(principal: Principal = Depends(require_roles(*_CREDIT_READERS)),
                     session: Session = Depends(get_session, scope="function")):
    """Every facility the caller can see, worst headroom first — the screen a
    finance desk opens in the morning."""
    ids = session.execute(
        text("SELECT id FROM credit_facility WHERE (CAST(:v AS TEXT) IS NULL OR vendor_id=:v) "
             "AND (CAST(:p AS TEXT) IS NULL OR pharmacy_id=:p)"),
        {"v": principal.vendor_id, "p": principal.pharmacy_id},
    ).scalars().all()
    views = [_facility_view(session, f) for f in ids]
    # `money_str` is a plain 2dp string (A2.4), so Decimal() reads it back
    # exactly — no locale parsing, and no float anywhere on this path.
    views.sort(key=lambda v: Decimal(v["headroom"]))
    return {"facility_count": len(views),
            "blocked_count": sum(1 for v in views if Decimal(v["headroom"]) <= 0),
            "items": views}
