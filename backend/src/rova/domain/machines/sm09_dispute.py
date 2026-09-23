"""SM-09 — dispute (A4.2 row SM-09). `RESOLVE` effects (firing the SM-03
transition chosen by `outcome`, R-076, and SM-11 `DEVIATION_RESOLVED` for
invoice disputes) are orchestrated by `rova/fulfilment/router.py`, which
calls this machine and then the others in the same transaction — a machine
module never reaches into another machine (A4.1).

Amendment (executor manifest, job-engine session): the spec's own SM-09
transition table (§6 SM-09) names `SlaTimer(dispute_resolution)` as a side
effect of OPEN -> UNDER_REVIEW ("start ... default A-10") and of
UNDER_REVIEW -> RESOLVED ("cancel timer") — R-124's own prose is explicit
that this timer "does not start until actual assignment" and that its
DISPUTE_RESOLUTION-days expiry is what drives UNDER_REVIEW -> ESCALATED
(fired by `rova/jobs/tick.py`, not this module, since a machine module never
starts a timer for the *next* transition of a machine it isn't currently
applying — but starting *this* transition's own outgoing timer is the same
pattern every other machine in this codebase uses, e.g. SM-02 SUBMIT). That
start/cancel was missing from both UNDER_REVIEW-entering transitions and
from RESOLVE; added here rather than left silently divergent from the spec
table cited by this same module's own docstring."""
from rova.core.clock import now
from rova.domain import timers
from rova.domain.enums import DisputeStatus, RoleCode
from rova.domain.fsm import GuardResult, Machine, Transition

_VF = frozenset({RoleCode.VENDOR_FINANCE, RoleCode.VENDOR_ADMIN})
_CF = frozenset({RoleCode.COMPLIANCE_OFFICER})
_SYSTEM = frozenset({"SYSTEM"})

SUBJECT = "dispute"
POLICY = "DISPUTE_RESOLUTION"

S = DisputeStatus


def _effect_start_resolution_timer(ctx) -> None:
    timers.start(ctx.session, policy_type=POLICY, subject_type=SUBJECT, subject_id=ctx.subject_id,
                 cfg_key="CFG-DISPUTE-RESOLUTION-DAYS", unit="DAYS")


def _effect_cancel_resolution_timer(ctx) -> None:
    timers.cancel(ctx.session, policy_type=POLICY, subject_type=SUBJECT, subject_id=ctx.subject_id)


def _guard_resolve(ctx) -> GuardResult:
    outcome = ctx.kwargs.get("outcome")
    if outcome not in ("CREDIT_NOTE", "REPLACEMENT", "REJECTED", "ESCALATED_TO_COMPLIANCE"):
        return GuardResult.failed("outcome is required and must be a valid DisputeOutcome", rule="R-076")
    actor_roles = frozenset({"SYSTEM"}) if ctx.actor == "SYSTEM" else ctx.actor.roles
    if ctx.subject_row["type"] == "REGULATED_PRICE_INCIDENT" and not (actor_roles & _CF):
        return GuardResult.failed("a REGULATED_PRICE_INCIDENT dispute can only be resolved by ComplianceOfficer",
                                   rule="R-074")
    return GuardResult.passed()


MACHINE = Machine(
    code="SM-09",
    subject_table="dispute",
    status_column="status",
    transitions=[
        Transition("SM-09", (S.OPEN,), S.UNDER_REVIEW, "ASSIGN", _VF | _CF,
                   effects=_effect_start_resolution_timer,
                   extra_set=lambda ctx: {"assigned_to_user_id": ctx.actor_user_id, "assigned_at": now()}),
        Transition("SM-09", (S.OPEN,), S.UNDER_REVIEW, "FORCE_ASSIGN", _SYSTEM, rule_refs=("R-124",),
                   effects=_effect_start_resolution_timer,
                   extra_set=lambda ctx: {"assigned_at": now()}),
        Transition("SM-09", (S.UNDER_REVIEW,), S.ESCALATED, "ESCALATE", _VF | _CF | _SYSTEM,
                   effects=_effect_cancel_resolution_timer),
        Transition("SM-09", (S.UNDER_REVIEW,), S.RESOLVED, "RESOLVE", _VF | _CF, guard=_guard_resolve,
                   rule_refs=("R-076",), effects=_effect_cancel_resolution_timer,
                   extra_set=lambda ctx: {"outcome": ctx.kwargs.get("outcome"), "notes": ctx.kwargs.get("notes"),
                                           "resolved_by_user_id": ctx.actor_user_id, "resolved_at": now()}),
        Transition("SM-09", (S.ESCALATED,), S.RESOLVED, "RESOLVE_ESCALATED", _CF,
                   extra_set=lambda ctx: {"outcome": ctx.kwargs.get("outcome"), "notes": ctx.kwargs.get("notes"),
                                           "resolved_by_user_id": ctx.actor_user_id, "resolved_at": now()}),
    ],
)
