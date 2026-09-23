"""SM-08 — licence (§6 SM-08, A4.2 row SM-08).

Implemented subset: the full trigger vocabulary (`OPEN_REVIEW`, `APPROVE`,
`REJECT`, `ENTER_WARNING_WINDOW`, `RENEW`, `EXPIRE`). `APPROVE`/`RENEW`/
`EXPIRE`/`ENTER_WARNING_WINDOW` fire the matching SM-06/SM-07 transition
(`APPROVE`/`LICENCE_RENEWED`/`LICENCE_EXPIRED`/`LICENCE_WARNING`) on the
licence's holder when the holder is a vendor or pharmacy account currently
in the state that transition expects — a transporter holder has no such
machine wired (A18) so nothing fires for it. `RENEW` links the old licence
to its successor by `successor_id` (the new `Licence` row, already `VALID`,
passed as `kwargs['successor_id']`)."""
from sqlalchemy import text

from rova.core.clock import now
from rova.domain.enums import LicenceStatus, RoleCode
from rova.domain.fsm import Ctx, GuardResult, Machine, Transition

_CF = frozenset({RoleCode.COMPLIANCE_OFFICER})

SUBJECT = "licence"


def _fire_holder(ctx: Ctx, trigger: str, *, from_status: str | None = None) -> None:
    """Fires `trigger` on the holder's SM-06/SM-07 machine when the holder
    is a vendor or pharmacy currently in `from_status` (or unconditionally
    when `from_status` is None) — never raises when the holder has already
    moved on or is a transporter (SM-08 is a record of the licence, not a
    strict driver of the holder machine).

    Forwards `ctx.actor` (the principal who drove *this* SM-08 transition)
    rather than hardcoding SYSTEM: SM-06/SM-07's own `APPROVE` accepts only
    ComplianceOfficer and `LICENCE_RENEWED` accepts SYSTEM or ComplianceOfficer,
    matching exactly the actor sets SM-08's `APPROVE`/`RENEW` transitions
    themselves already require — so this always satisfies the holder
    machine's own actor check instead of a hardcoded "SYSTEM" spuriously
    failing FORBIDDEN on a ComplianceOfficer-driven approval/renewal.
    `ENTER_WARNING_WINDOW`/`EXPIRE` are SYSTEM-only on SM-08 already, so
    forwarding is a no-op change for those two."""
    holder_type = ctx.subject_row["holder_type"]
    holder_id = ctx.subject_row["holder_id"]
    table = {"VENDOR": "vendor_account", "PHARMACY": "pharmacy_account"}.get(holder_type)
    if table is None:
        return
    current = ctx.session.execute(text(f'SELECT status FROM {table} WHERE id=:h'), {"h": holder_id}).scalar()
    if current is None or (from_status is not None and current != from_status):
        return
    from rova.domain.machines.registry import MACHINES
    machine_code = "SM-06" if holder_type == "VENDOR" else "SM-07"
    MACHINES[machine_code].apply(ctx.session, holder_id, trigger, ctx.actor)


def _effect_approve(ctx: Ctx) -> None:
    _fire_holder(ctx, "APPROVE", from_status="ONBOARDING")


def _guard_renew(ctx: Ctx) -> GuardResult:
    successor_id = ctx.kwargs.get("successor_id")
    if not successor_id:
        return GuardResult.failed("successor_id is required")
    status = ctx.session.execute(text("SELECT status FROM licence WHERE id=:s"), {"s": successor_id}).scalar()
    if status != LicenceStatus.VALID:
        return GuardResult.failed("successor licence must already be VALID")
    return GuardResult.passed()


def _effect_renew(ctx: Ctx) -> None:
    _fire_holder(ctx, "LICENCE_RENEWED", from_status="LICENCE_EXPIRING")


def _guard_expire(ctx: Ctx) -> GuardResult:
    if ctx.subject_row["expiry_date"] >= now().date():
        return GuardResult.failed("expiry_date has not passed yet")
    return GuardResult.passed()


def _effect_expire(ctx: Ctx) -> None:
    _fire_holder(ctx, "LICENCE_EXPIRED", from_status="LICENCE_EXPIRING")


def _effect_enter_warning(ctx: Ctx) -> None:
    _fire_holder(ctx, "LICENCE_WARNING", from_status="ACTIVE")


def _guard_warning_window(ctx: Ctx) -> GuardResult:
    from rova.config_params import service as cfg
    from datetime import timedelta
    days = cfg.get(ctx.session, "CFG-LICENCE-EXPIRY-WARNING-DAYS", default=30)
    if ctx.subject_row["expiry_date"] > now().date() + timedelta(days=days):
        return GuardResult.failed("not yet within CFG-LICENCE-EXPIRY-WARNING-DAYS of expiry_date")
    return GuardResult.passed()


MACHINE = Machine(
    code="SM-08",
    subject_table=SUBJECT,
    status_column="status",
    transitions=[
        Transition("SM-08", (LicenceStatus.SUBMITTED,), LicenceStatus.UNDER_REVIEW, "OPEN_REVIEW", _CF),
        Transition("SM-08", (LicenceStatus.UNDER_REVIEW,), LicenceStatus.VALID, "APPROVE", _CF,
                   effects=_effect_approve),
        Transition("SM-08", (LicenceStatus.UNDER_REVIEW,), LicenceStatus.REJECTED, "REJECT", _CF),
        Transition("SM-08", (LicenceStatus.VALID,), LicenceStatus.EXPIRING_SOON, "ENTER_WARNING_WINDOW",
                   frozenset({"SYSTEM"}), guard=_guard_warning_window, effects=_effect_enter_warning),
        Transition("SM-08", (LicenceStatus.EXPIRING_SOON,), LicenceStatus.RENEWED, "RENEW", _CF,
                   guard=_guard_renew, effects=_effect_renew,
                   extra_set=lambda ctx: {"successor_id": ctx.kwargs.get("successor_id")}),
        Transition("SM-08", (LicenceStatus.EXPIRING_SOON,), LicenceStatus.EXPIRED, "EXPIRE",
                   frozenset({"SYSTEM"}), guard=_guard_expire, effects=_effect_expire),
    ],
)
