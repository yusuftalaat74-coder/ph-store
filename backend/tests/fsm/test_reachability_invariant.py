"""A17 — generic, programmatic reachability/exit-invariant check (A4.1's own
closing rule: every state reachable, every state has an exit or is declared
terminal, every transition names trigger/actor/guard/side-effects) run over
every registered `Machine`, not asserted by hand per machine.

For each machine:
  - a state is 'reachable' when it is either the subject table's own column
    DEFAULT (created directly by a raw INSERT outside the state-machine
    layer, e.g. checkout.py's `INSERT INTO "order" ... status=
    'PENDING_ACCEPTANCE'`) or appears as some transition's `to_state`;
  - a state 'has an exit' when it appears in some transition's `from_states`,
    or is declared terminal in TERMINAL_STATES below.

Every registered machine, SM-01 included, satisfies the invariant with ZERO
exemptions, which this file asserts, not merely claims. SM-01 used to carry
a named exemption here for three states (`AWAITING_ADMIN_APPROVAL`,
`NORMALIZING`, `AWAITING_CONFIRMATION`) that its own transitions table left
genuinely unreachable and exit-less; backend-review-r1.md item 6 flagged
that exemption as exactly what let SM-01 stay green while dead — it has
been removed, and `rova/domain/machines/sm01_request.py` now wires the
missing transitions for real (see that module's own docstring)."""
import pytest

from rova.domain.enums import ENUM_REGISTRY
from rova.domain.machines.registry import MACHINES

# subject_table.status_column's DEFAULT at row creation (migrations/sql/
# 0001_initial.sql) — reachable without any Transition, exactly like every
# machine's own "STATES ... terminal? no/yes" table treats its first state.
INITIAL_STATE = {
    "SM-01": "DRAFT",
    "SM-02": "INVITED",
    "SM-03": "PENDING_ACCEPTANCE",
    "SM-04": "LINE_PENDING",
    "SM-05": "CREATED",
    "SM-06": "ONBOARDING",
    "SM-07": "ONBOARDING",
    "SM-08": "SUBMITTED",
    "SM-09": "OPEN",
    "SM-10": "REQUESTED",
    "SM-11": "UPLOADED",
    "SM-12": "FRESH",
    "SM-20": "PROVISIONAL",
    "SM-21": "UPLOADED",
    "SM-22": "STEADY",
    "SM-23": "PENDING_CONSENT",
    "SM-24": "BOT",
}

# Declared terminal states per machine (§6/addendum "STATES ... terminal?"
# tables) — a terminal state legitimately has no outgoing transition.
TERMINAL_STATES = {
    "SM-01": {"CANCELLED", "CLOSED"},
    "SM-02": {"ACCEPTED", "DECLINED", "EXPIRED"},
    "SM-03": {"CANCELLED", "CLOSED", "REJECTED"},
    "SM-04": {"LINE_DROPPED", "LINE_REROUTED"},
    "SM-05": {"CANCELLED", "DELIVERED", "RETURNED_TO_VENDOR"},
    "SM-06": {"CLOSED"},
    "SM-07": {"CLOSED"},
    "SM-08": {"EXPIRED", "REJECTED", "RENEWED"},
    "SM-09": {"RESOLVED"},
    "SM-10": {"CLOSED", "REJECTED"},
    "SM-11": {"PAID", "WRITTEN_OFF"},
    "SM-12": {"WITHDRAWN"},
    "SM-20": {"MISSED", "REALISED", "VOID"},
    "SM-21": {"REJECTED", "SUPERSEDED"},
    "SM-22": set(),  # a steady-state cycle, no machine-level terminal state
    "SM-23": {"REVOKED"},
    "SM-24": set(),  # BOT/ESCALATING/HUMAN cycle, no terminal state
}

# States this backend's pre-existing reduced scope leaves genuinely dead
# (both unreachable and exit-less), named here rather than silently
# swallowed by the invariant checks below. Empty: every machine, including
# SM-01 (item 6), now satisfies the invariant with zero exemptions.
REDUCED_SCOPE_EXEMPT: dict[str, set[str]] = {}


def test_every_registered_machine_is_covered_by_this_invariant():
    """Guards the tables above themselves: a machine added to MACHINES later
    without a matching INITIAL_STATE/TERMINAL_STATES entry must fail loudly
    here instead of the parametrized tests below silently skipping it."""
    assert set(MACHINES) == set(INITIAL_STATE) == set(TERMINAL_STATES)
    assert set(REDUCED_SCOPE_EXEMPT) <= set(MACHINES)


@pytest.mark.parametrize("code", sorted(MACHINES))
def test_every_state_reachable(code):
    machine = MACHINES[code]
    enum_cls = ENUM_REGISTRY[(machine.subject_table, machine.status_column)]
    all_states = {e.value for e in enum_cls}
    to_states = {t.to_state for t in machine.transitions}
    reachable = to_states | {INITIAL_STATE[code]} | REDUCED_SCOPE_EXEMPT.get(code, set())
    unreachable = all_states - reachable
    assert not unreachable, (
        f"{code}: state(s) {sorted(unreachable)} have neither a column DEFAULT "
        f"nor any transition producing them (A4.1 reachability invariant)"
    )


@pytest.mark.parametrize("code", sorted(MACHINES))
def test_every_state_has_exit_or_is_terminal(code):
    machine = MACHINES[code]
    enum_cls = ENUM_REGISTRY[(machine.subject_table, machine.status_column)]
    all_states = {e.value for e in enum_cls}
    from_states: set[str] = set()
    for t in machine.transitions:
        from_states.update(t.from_states)
    covered = from_states | TERMINAL_STATES[code] | REDUCED_SCOPE_EXEMPT.get(code, set())
    dead_ends = all_states - covered
    assert not dead_ends, (
        f"{code}: state(s) {sorted(dead_ends)} have no outgoing transition and "
        f"are not declared terminal (A4.1 exit invariant)"
    )


def test_transitions_reference_only_known_states():
    """A typo'd from_state/to_state string would otherwise just silently
    make a transition permanently dead rather than raising anywhere — every
    state named in every transition must belong to that machine's own DDL-
    backed status enum."""
    for code, machine in MACHINES.items():
        enum_cls = ENUM_REGISTRY[(machine.subject_table, machine.status_column)]
        all_states = {e.value for e in enum_cls}
        for t in machine.transitions:
            assert set(t.from_states) <= all_states, f"{code}.{t.trigger}: unknown from_state(s)"
            assert t.to_state in all_states, f"{code}.{t.trigger}: unknown to_state {t.to_state!r}"


def test_every_transition_names_trigger_actor_and_declares_a_guard_or_none_deliberately():
    """A4.2's own requirement — 'every transition names trigger/actor/guard/
    side-effects' — checked structurally: `trigger` is a non-empty string,
    `actors` is a non-empty set (SYSTEM counts), and `guard`/`effects` are
    either a callable or explicitly None (never silently absent as an
    unset attribute — the dataclass default already guarantees this, this
    test guards against that default ever being loosened)."""
    for code, machine in MACHINES.items():
        for t in machine.transitions:
            assert isinstance(t.trigger, str) and t.trigger, f"{code}: transition with no trigger name"
            assert t.actors, f"{code}.{t.trigger}: no actors declared"
            assert t.guard is None or callable(t.guard)
            assert t.effects is None or callable(t.effects)
