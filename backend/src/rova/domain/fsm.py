"""A4.1 — the one generic state-machine engine. State machines are enforced
in the application layer, in this module, and nowhere else: the database
stores the current state (CHECK on the value set) and the append-only
`state_transition` log, but does not know which transitions are legal.

Every machine is a module `rova/domain/machines/smNN_<name>.py` exporting a
`Machine` built from `Transition`s (A4.2). `Machine.apply` does, in order:
lock the subject row FOR UPDATE; find (from_state, trigger) or raise
ILLEGAL_TRANSITION; check the actor's roles or raise FORBIDDEN
(ACTOR_NOT_ALLOWED); run the guard or raise GUARD_FAILED; UPDATE ... WHERE
status = :expected (STALE_STATE on zero rows); insert state_transition;
run effects; run cross-cutting hooks registered for (machine, to_state).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.core.clock import now
from rova.core.errors import ApiError
from rova.core.ids import new_id

SYSTEM = "SYSTEM"


@dataclass(frozen=True)
class Principal:
    """Minimal shape the FSM engine needs; rova.auth.principal.Principal is a
    superset and satisfies this by duck typing (roles, user_id)."""
    user_id: str | None
    roles: frozenset[str]


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reason: str | None = None
    rule: str | None = None

    @staticmethod
    def passed() -> "GuardResult":
        return GuardResult(ok=True)

    @staticmethod
    def failed(reason: str, rule: str | None = None) -> "GuardResult":
        return GuardResult(ok=False, reason=reason, rule=rule)


@dataclass(frozen=True)
class Ctx:
    session: Session
    actor: Principal | str  # Principal, or the literal "SYSTEM"
    subject_id: str
    subject_row: dict[str, Any]
    kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def actor_role(self) -> str:
        if self.actor == SYSTEM:
            return "SYSTEM"
        return next(iter(self.actor.roles), "UNKNOWN")

    @property
    def actor_user_id(self) -> str | None:
        if self.actor == SYSTEM:
            return None
        return self.actor.user_id


@dataclass(frozen=True)
class Transition:
    machine: str
    from_states: tuple[str, ...]  # transition legal from any of these states
    to_state: str
    trigger: str
    actors: frozenset[str]  # role codes, or {"SYSTEM"}
    guard: Callable[[Ctx], GuardResult] | None = None
    effects: Callable[[Ctx], None] | None = None
    rule_refs: tuple[str, ...] = ()
    # extra columns to SET in the same UPDATE statement as the status change
    # (needed for CHECKs like (status='SUSPENDED') = (suspension_cause IS NOT NULL),
    # which Postgres evaluates immediately per statement, not deferred).
    extra_set: Callable[[Ctx], dict] | None = None


# hooks registered as (machine, to_state) -> [fn(ctx)]; populated by
# rova/domain/hooks.py so a machine module never imports fees/eta/etc.
_HOOKS: dict[tuple[str, str], list[Callable[[Ctx], None]]] = {}


def register_hook(machine: str, to_state: str, fn: Callable[[Ctx], None]) -> None:
    _HOOKS.setdefault((machine, to_state), []).append(fn)


class Machine:
    def __init__(self, code: str, subject_table: str, status_column: str, transitions: list[Transition]):
        self.code = code
        self.subject_table = subject_table
        self.status_column = status_column
        self.transitions = transitions

    def legal_triggers_from(self, state: str) -> list[str]:
        return [t.trigger for t in self.transitions if state in t.from_states]

    def _find(self, state: str, trigger: str) -> Transition | None:
        for t in self.transitions:
            if trigger == t.trigger and state in t.from_states:
                return t
        return None

    def apply(self, session: Session, subject_id: str, trigger: str, actor: Principal | str, **kwargs) -> dict:
        row = session.execute(
            text(f'SELECT * FROM "{self.subject_table}" WHERE id = :id FOR UPDATE'),
            {"id": subject_id},
        ).mappings().first()
        if row is None:
            raise ApiError("NOT_FOUND", f"{self.subject_table} not found")
        current = row[self.status_column]

        transition = self._find(current, trigger)
        if transition is None:
            legal = self.legal_triggers_from(current)
            raise ApiError(
                "ILLEGAL_TRANSITION",
                f"{trigger} is not legal from {self.subject_table}.{self.status_column}={current}",
                details=[{"field": "trigger", "reason": f"legal triggers from this state: {legal}"}],
            )

        actor_roles = frozenset({SYSTEM}) if actor == SYSTEM else actor.roles
        if not (actor_roles & transition.actors):
            raise ApiError(
                "FORBIDDEN", "actor role not allowed for this transition",
                details=[{"field": "ACTOR_NOT_ALLOWED", "reason": str(sorted(transition.actors))}],
            )

        ctx = Ctx(session=session, actor=actor, subject_id=subject_id, subject_row=dict(row), kwargs=kwargs)

        if transition.guard is not None:
            result = transition.guard(ctx)
            if not result.ok:
                raise ApiError("GUARD_FAILED", result.reason or "guard failed", rule=result.rule)

        extra = transition.extra_set(ctx) if transition.extra_set else {}
        extra_sql = "".join(f", {col} = :extra_{col}" for col in extra)
        params = {"to_state": transition.to_state, "now": now(), "id": subject_id, "expected": current}
        params.update({f"extra_{col}": val for col, val in extra.items()})
        updated = session.execute(
            text(
                f'UPDATE "{self.subject_table}" SET {self.status_column} = :to_state, updated_at = :now{extra_sql} '
                f"WHERE id = :id AND {self.status_column} = :expected"
            ),
            params,
        )
        if updated.rowcount == 0:
            raise ApiError("STALE_STATE", "subject changed concurrently; retry")

        session.execute(
            text(
                "INSERT INTO state_transition (id, machine, subject_type, subject_id, from_state, to_state, "
                "trigger, actor_user_id, actor_role, notes, occurred_at) "
                "VALUES (:id, :machine, :subject_type, :subject_id, :from_state, :to_state, :trigger, "
                ":actor_user_id, :actor_role, CAST(:notes AS JSONB), :occurred_at)"
            ),
            {
                "id": new_id("stt"),
                "machine": self.code,
                "subject_type": self.subject_table,
                "subject_id": subject_id,
                "from_state": current,
                "to_state": transition.to_state,
                "trigger": trigger,
                "actor_user_id": ctx.actor_user_id,
                "actor_role": ctx.actor_role,
                "notes": "{}",
                "occurred_at": now(),
            },
        )

        # refresh subject_row with the new status for effects/hooks
        ctx = Ctx(
            session=session, actor=actor, subject_id=subject_id,
            subject_row={**ctx.subject_row, self.status_column: transition.to_state},
            kwargs=kwargs,
        )

        if transition.effects is not None:
            transition.effects(ctx)

        for hook in _HOOKS.get((self.code, transition.to_state), []):
            hook(ctx)

        return session.execute(
            text(f'SELECT * FROM "{self.subject_table}" WHERE id = :id'), {"id": subject_id}
        ).mappings().one()
