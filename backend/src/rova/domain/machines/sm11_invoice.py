"""SM-11 — invoice (A4.2 row SM-11). The spec's single `MATCH` trigger
branches to `PRICE_MATCH_OK` or `PRICE_DEVIATION_FLAGGED`; the generic engine
(A4.1) needs one `to_state` per (from_state, trigger) pair, so the price
match is computed by `rova/billing/router.py` *before* calling `apply()`,
which then picks the already-decided trigger — the same pattern
`fulfilment/router.py::accept_order` already uses to choose `ACCEPT` vs
`REJECT` for SM-03. `FINALISE` runs automatically right after a `MATCH_OK`
(no human step between, per A4.2); a flagged deviation instead waits for
`DEVIATION_RESOLVED` (fired by SM-09 `RESOLVE`, R-076) before the same
`ledger_entry(INVOICE)` effect runs.

Item-3 fix (backend-review-r1.md): `FINALISE`/`DEVIATION_RESOLVED` had no
effects at all — the credit gate could never release exposure because no
`ledger_entry(INVOICE)` row was ever written (`pending_exposure` only
excludes an order once its invoice reaches `price_match_flag='OK'`, and
`current_exposure` only grows once that INVOICE entry lands, A10/A3.3).
`WRITE_OFF` now writes the ADJUSTMENT contra-entry A10 names for it."""
from sqlalchemy import text

from rova.core.clock import now
from rova.core.ids import new_id
from rova.domain.enums import InvoiceStatus, RoleCode
from rova.domain.fsm import Ctx, Machine, Transition

_VF = frozenset({RoleCode.VENDOR_FINANCE, RoleCode.VENDOR_ADMIN})
_SYSTEM = frozenset({"SYSTEM"})
_CF = frozenset({RoleCode.COMPLIANCE_OFFICER})

S = InvoiceStatus


def _facility_id(ctx: Ctx) -> str | None:
    return ctx.session.execute(
        text("SELECT id FROM credit_facility WHERE vendor_id=:v AND pharmacy_id=:p"),
        {"v": ctx.subject_row["issuer_vendor_id"], "p": ctx.subject_row["pharmacy_id"]},
    ).scalar()


def _effect_finalise(ctx: Ctx) -> None:
    """Defect 5 fix: `ledger_entry(INVOICE)` is what `current_exposure` reads
    (A10/A3.3) — it is a *credit* mechanism. A pharmacy on `UPFRONT` or `COD`
    terms pays cash at (or independent of) invoicing, not against a credit
    facility, and very often chose `UPFRONT` in the first place *because* it
    had no credit headroom left (J-20). Writing this entry regardless of
    `order.payment_terms` consumed that headroom anyway — a cash invoice
    should never move `current_exposure` for a facility the order never
    drew on. Only `CREDIT_N_DAYS` orders write this entry; the architect's
    ruling (six-defect repair round) is to skip it outright for UPFRONT and
    COD rather than write and immediately reverse it."""
    order = ctx.session.execute(
        text('SELECT payment_terms FROM "order" WHERE id=:o'), {"o": ctx.subject_row["order_id"]}
    ).mappings().one()
    if order["payment_terms"] != "CREDIT_N_DAYS":
        return
    facility_id = _facility_id(ctx)
    if facility_id is None:
        return
    ctx.session.execute(
        text(
            "INSERT INTO ledger_entry (id, credit_facility_id, entry_type, reference_type, reference_id, amount) "
            "VALUES (:id, :f, 'INVOICE', 'invoice', :ref, :amt)"
        ),
        {"id": new_id("led"), "f": facility_id, "ref": ctx.subject_id, "amt": ctx.subject_row["total_amount"]},
    )


def _effect_write_off(ctx: Ctx) -> None:
    """Mirrors the defect-5 guard in `_effect_finalise`: only a
    `CREDIT_N_DAYS` invoice ever got an `INVOICE` ledger entry in the first
    place, so only that kind has anything to contra-write-off here — writing
    an `ADJUSTMENT` against a facility for an UPFRONT/COD invoice that never
    drew on it would corrupt `current_exposure` the other direction."""
    order = ctx.session.execute(
        text('SELECT payment_terms FROM "order" WHERE id=:o'), {"o": ctx.subject_row["order_id"]}
    ).mappings().one()
    if order["payment_terms"] != "CREDIT_N_DAYS":
        return
    facility_id = _facility_id(ctx)
    if facility_id is None:
        return
    outstanding = ctx.session.execute(
        text(
            "SELECT :total - COALESCE((SELECT SUM(amount) FROM payment_allocation WHERE invoice_id=:i), 0)"
        ),
        {"total": ctx.subject_row["total_amount"], "i": ctx.subject_id},
    ).scalar()
    if not outstanding or outstanding <= 0:
        return
    ctx.session.execute(
        text(
            "INSERT INTO ledger_entry (id, credit_facility_id, entry_type, reference_type, reference_id, amount) "
            "VALUES (:id, :f, 'ADJUSTMENT', 'invoice_write_off', :ref, :amt)"
        ),
        {"id": new_id("led"), "f": facility_id, "ref": ctx.subject_id, "amt": -outstanding},
    )
    ctx.session.execute(
        text(
            "INSERT INTO audit_event (id, actor_user_id, actor_role, action_code, subject_type, subject_id, "
            "metadata) VALUES (:id, :uid, :role, 'INVOICE_WRITTEN_OFF', 'invoice', :sid, '{}')"
        ),
        {"id": new_id("aud"), "uid": ctx.actor_user_id, "role": ctx.actor_role, "sid": ctx.subject_id},
    )


MACHINE = Machine(
    code="SM-11",
    subject_table="invoice",
    status_column="status",
    transitions=[
        Transition("SM-11", (S.UPLOADED,), S.PRICE_MATCH_OK, "MATCH_OK", _SYSTEM,
                   extra_set=lambda ctx: {"price_match_flag": "OK"}),
        Transition("SM-11", (S.UPLOADED,), S.PRICE_DEVIATION_FLAGGED, "MATCH_DEVIATION", _SYSTEM,
                   extra_set=lambda ctx: {"price_match_flag": ctx.kwargs.get("price_match_flag", "DEVIATION_ABOVE")}),
        Transition("SM-11", (S.PRICE_MATCH_OK,), S.AWAITING_PAYMENT, "FINALISE", _SYSTEM,
                   effects=_effect_finalise, extra_set=lambda ctx: {"finalised_at": now()}),
        Transition("SM-11", (S.PRICE_DEVIATION_FLAGGED,), S.AWAITING_PAYMENT, "DEVIATION_RESOLVED", _SYSTEM | _CF,
                   effects=_effect_finalise,
                   extra_set=lambda ctx: {"finalised_at": now(), "price_match_flag": "OK"}),
        Transition("SM-11", (S.AWAITING_PAYMENT,), S.PARTIALLY_PAID, "ALLOCATE_PARTIAL", _SYSTEM),
        Transition("SM-11", (S.PARTIALLY_PAID,), S.PARTIALLY_PAID, "ALLOCATE_PARTIAL", _SYSTEM),
        Transition("SM-11", (S.AWAITING_PAYMENT,), S.PAID, "ALLOCATE_FULL", _SYSTEM),
        Transition("SM-11", (S.PARTIALLY_PAID,), S.PAID, "ALLOCATE_FULL", _SYSTEM),
        Transition("SM-11", (S.AWAITING_PAYMENT, S.PARTIALLY_PAID), S.WRITTEN_OFF, "WRITE_OFF", _VF,
                   effects=_effect_write_off),
    ],
)
