"""SM-22 — mode_switch (addendum §A5/§A7, A4.2 row SM-22, R-140).

Implemented subset: the full six-state cycle. R-140 ("no path writes
`current_value` outside `SM-22`") is structural here: `current_value` is set
only inside this module's `FLIP` and `ROLLBACK` transitions' `extra_set`,
both reached only through `Machine.apply` (never a raw `UPDATE mode_switch`
anywhere else in the codebase — grepped by
`tests/fsm/test_sm22_mode_switch.py`). Gates are evaluated by
`rova/modes/gates.py` (reduced to `G4` for `VENDOR_MODE -> MULTI`, see that
module's docstring for the cut)."""
from sqlalchemy import text

from rova.core.clock import now
from rova.core.ids import new_id
from rova.domain.enums import ModeSwitchStatus, RoleCode
from rova.domain.fsm import Ctx, GuardResult, Machine, Transition
from rova.modes.gates import evaluate_gates

_ADMIN = frozenset({RoleCode.PLATFORM_ADMIN})

SUBJECT = "mode_switch"


def _effect_propose(ctx: Ctx) -> None:
    pass


def _effect_check_gates(ctx: Ctx) -> None:
    results = evaluate_gates(ctx.session, ctx.subject_row["switch_key"], ctx.subject_row["proposed_value"])
    import json
    payload = [{"gate_id": r.gate_id, "passed": r.passed, "evidence_ref": r.evidence_ref, "checked_at": now().isoformat()}
               for r in results]
    ctx.session.execute(
        text("UPDATE mode_switch SET gate_results=CAST(:g AS JSONB), gates_checked_at=:n, updated_at=:n WHERE id=:id"),
        {"g": json.dumps(payload), "n": now(), "id": ctx.subject_id},
    )


def _all_gates_passed(ctx: Ctx) -> bool:
    row = ctx.session.execute(text("SELECT gate_results FROM mode_switch WHERE id=:id"), {"id": ctx.subject_id}).mappings().one()
    results = row["gate_results"] or []
    return all(g["passed"] for g in results)


def _guard_gates_passed(ctx: Ctx) -> GuardResult:
    if not _all_gates_passed(ctx):
        return GuardResult.failed("at least one gate failed", rule="R-140")
    return GuardResult.passed()


def _guard_gates_failed(ctx: Ctx) -> GuardResult:
    if _all_gates_passed(ctx):
        return GuardResult.failed("all gates passed; use GATES_PASSED")
    return GuardResult.passed()


def _guard_flip(ctx: Ctx) -> GuardResult:
    from datetime import timedelta
    from rova.config_params import service as cfg
    if ctx.subject_row["gates_checked_at"] is None:
        return GuardResult.failed("gates have never been checked", rule="R-140")
    max_age = cfg.get(ctx.session, "CFG-SWITCH-GATE-MAX-AGE-HOURS", default=72)
    if ctx.subject_row["gates_checked_at"] < now() - timedelta(hours=float(max_age)):
        return GuardResult.failed("gate results are older than CFG-SWITCH-GATE-MAX-AGE-HOURS; re-check", rule="R-140")
    return GuardResult.passed()


def _effect_flip(ctx: Ctx) -> None:
    from datetime import timedelta
    from rova.config_params import service as cfg
    days = cfg.get(ctx.session, "CFG-SWITCH-OBSERVATION-DAYS", default=14)
    ctx.session.execute(
        text(
            "INSERT INTO audit_event (id, actor_role, actor_user_id, action_code, subject_type, subject_id, "
            "metadata) VALUES (:id, :role, :uid, 'MODE_SWITCH_FLIPPED', :st, :sid, "
            "CAST(:meta AS JSONB))"
        ),
        {"id": new_id("aud"), "role": ctx.actor_role, "uid": ctx.actor_user_id, "st": SUBJECT,
         "sid": ctx.subject_id, "meta": '{"observation_days": %s}' % days},
    )


def _extra_flip(ctx: Ctx) -> dict:
    from datetime import timedelta
    from rova.config_params import service as cfg
    days = cfg.get(ctx.session, "CFG-SWITCH-OBSERVATION-DAYS", default=14)
    return {
        "current_value": ctx.subject_row["proposed_value"],
        "previous_value": ctx.subject_row["current_value"],
        "flipped_by_user_id": ctx.actor_user_id,
        "flipped_at": now(),
        "observation_ends_at": now() + timedelta(days=float(days)),
    }


def _guard_observation_elapsed(ctx: Ctx) -> GuardResult:
    if ctx.subject_row["observation_ends_at"] is None or ctx.subject_row["observation_ends_at"] > now():
        return GuardResult.failed("observation window has not elapsed")
    return GuardResult.passed()


def _guard_rollback(ctx: Ctx) -> GuardResult:
    if ctx.subject_row["previous_value"] is None:
        return GuardResult.failed("no previous_value to roll back to")
    return GuardResult.passed()


def _effect_rollback(ctx: Ctx) -> None:
    ctx.session.execute(
        text(
            "INSERT INTO audit_event (id, actor_role, actor_user_id, action_code, subject_type, subject_id) "
            "VALUES (:id, :role, :uid, 'MODE_SWITCH_ROLLED_BACK', :st, :sid)"
        ),
        {"id": new_id("aud"), "role": ctx.actor_role, "uid": ctx.actor_user_id, "st": SUBJECT, "sid": ctx.subject_id},
    )


MACHINE = Machine(
    code="SM-22",
    subject_table=SUBJECT,
    status_column="status",
    transitions=[
        Transition("SM-22", (ModeSwitchStatus.STEADY,), ModeSwitchStatus.PROPOSED, "PROPOSE", _ADMIN,
                   extra_set=lambda ctx: {"proposed_value": ctx.kwargs.get("proposed_value"),
                                           "proposed_by_user_id": ctx.actor_user_id}),
        Transition("SM-22", (ModeSwitchStatus.PROPOSED,), ModeSwitchStatus.GATE_CHECKING, "CHECK_GATES",
                   frozenset({"SYSTEM"}), effects=_effect_check_gates),
        Transition("SM-22", (ModeSwitchStatus.GATE_CHECKING,), ModeSwitchStatus.READY, "GATES_PASSED",
                   frozenset({"SYSTEM"}), guard=_guard_gates_passed),
        Transition("SM-22", (ModeSwitchStatus.GATE_CHECKING,), ModeSwitchStatus.PROPOSED, "GATES_FAILED",
                   frozenset({"SYSTEM"}), guard=_guard_gates_failed),
        Transition("SM-22", (ModeSwitchStatus.READY,), ModeSwitchStatus.FLIPPED, "FLIP", _ADMIN,
                   guard=_guard_flip, effects=_effect_flip, extra_set=_extra_flip, rule_refs=("R-140",)),
        Transition("SM-22", (ModeSwitchStatus.FLIPPED,), ModeSwitchStatus.STEADY, "OBSERVATION_ELAPSED",
                   frozenset({"SYSTEM"}), guard=_guard_observation_elapsed,
                   extra_set=lambda ctx: {"previous_value": None}),
        Transition("SM-22", (ModeSwitchStatus.FLIPPED,), ModeSwitchStatus.ROLLED_BACK, "ROLLBACK", _ADMIN,
                   guard=_guard_rollback, effects=_effect_rollback,
                   extra_set=lambda ctx: {"current_value": ctx.subject_row["previous_value"]}),
        Transition("SM-22", (ModeSwitchStatus.ROLLED_BACK,), ModeSwitchStatus.STEADY, "ROLLBACK_ACK",
                   frozenset({"SYSTEM"})),
    ],
)
