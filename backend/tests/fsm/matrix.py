"""Item 5 (backend-review-r1.md): 'Generate the illegal-transition matrix
programmatically from MACHINE.transitions — every (state, trigger) pair not
in the table must be rejected. Do not hand-write it.' Shared by every new
fsm/test_smNN_*.py file this session adds (not a test module itself — no
`test_` prefix, so pytest does not collect it).

`illegal_pairs` derives the full Cartesian product of (every state the
machine's own status enum declares) x (every trigger name appearing
anywhere in MACHINE.transitions), minus the legal (state, trigger) pairs
MACHINE.transitions actually declares. `assert_all_illegal` then drives the
subject directly into each such state (bypassing the FSM — that's the
point, it's the state a caller could never legally reach for that trigger)
and asserts `MACHINE.apply` rejects every one of them with ILLEGAL_TRANSITION,
using whatever actor happens to be passed: `Machine.apply`'s own transition
lookup (`_find`) raises ILLEGAL_TRANSITION before it ever looks at the
actor's role, so which actor is used here doesn't matter — resolved from
`rova.domain.enums.ENUM_REGISTRY`, the same source the reachability
invariant test already trusts, not a hand-typed state list."""
import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.domain.enums import ENUM_REGISTRY


def illegal_pairs(machine) -> list[tuple[str, str]]:
    enum_cls = ENUM_REGISTRY[(machine.subject_table, machine.status_column)]
    all_states = {e.value for e in enum_cls}
    all_triggers = {t.trigger for t in machine.transitions}
    legal = {(state, t.trigger) for t in machine.transitions for state in t.from_states}
    return sorted((s, trig) for s in all_states for trig in all_triggers if (s, trig) not in legal)


def assert_all_illegal(session, machine, subject_id: str, actor, *, extra_by_state: dict[str, dict] | None = None) -> None:
    """`extra_by_state` — {state: {column: value}} — is for the handful of
    states some subject tables' own CHECK constraints tie to a companion
    column (e.g. dispute's `ck_dispute_resolved`: RESOLVED requires
    `outcome`/`resolved_at`; vendor_account/pharmacy_account's own
    `ck_*_suspension`: SUSPENDED requires `suspension_cause`) — forcing the
    subject into that state to probe an illegal trigger must satisfy the
    same constraint a legal transition's own `extra_set` would."""
    extra_by_state = extra_by_state or {}
    # every column named in ANY state's extra must be explicitly reset (to
    # NULL, by default) for every OTHER state too — several of these CHECK
    # constraints are a strict biconditional ("(status = X) = (col IS NOT
    # NULL)"), so a value left over from a previous iteration's UPDATE would
    # violate the constraint for a state that doesn't call for it.
    all_extra_cols = {col for cols in extra_by_state.values() for col in cols}
    pairs = illegal_pairs(machine)
    assert pairs, "machine has no illegal pairs to check — every trigger legal from every state?"
    for state, trigger in pairs:
        extra = {col: None for col in all_extra_cols} | extra_by_state.get(state, {})
        extra_sql = "".join(f", {col} = :extra_{col}" for col in extra)
        params = {"s": state, "id": subject_id, **{f"extra_{col}": val for col, val in extra.items()}}
        session.execute(
            text(f'UPDATE "{machine.subject_table}" SET {machine.status_column} = :s{extra_sql} WHERE id = :id'),
            params,
        )
        with pytest.raises(ApiError) as exc:
            machine.apply(session, subject_id, trigger, actor)
        assert exc.value.code == "ILLEGAL_TRANSITION", (
            f"{machine.code}: ({state}, {trigger}) should be illegal, got {exc.value.code}"
        )
