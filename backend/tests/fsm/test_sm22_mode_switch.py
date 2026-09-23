"""A17 'Every state machine' row for SM-22 (addendum §A5/§A7, R-140) —
mode_switch.status: PROPOSE, CHECK_GATES, GATES_PASSED/GATES_FAILED, FLIP
(guarded by CFG-SWITCH-GATE-MAX-AGE-HOURS freshness, R-140), OBSERVATION_
ELAPSED, ROLLBACK, ROLLBACK_ACK. `PRIMARY_INTERFACE` has no gates wired
(rova/modes/gates.py, reduced scope) so it passes trivially; `VENDOR_MODE ->
MULTI` exercises the one real gate, G4, which fails with zero ACTIVE vendors."""
import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

MACHINE = MACHINES["SM-22"]

PLATFORM_ADMIN = Principal(user_id=None, roles=frozenset({"PlatformAdmin"}))
COMPLIANCE = Principal(user_id=None, roles=frozenset({"ComplianceOfficer"}))


@pytest.fixture
def switch(session):
    session.execute(text(
        "INSERT INTO mode_switch (id, switch_key, scope_type, current_value, status) VALUES "
        "('mds_sm22_pi', 'PRIMARY_INTERFACE', 'GLOBAL', 'CONVERSATIONAL', 'STEADY')"
    ))
    yield "mds_sm22_pi"
    session.rollback()


@pytest.fixture
def vendor_switch(session):
    session.execute(text(
        "INSERT INTO mode_switch (id, switch_key, scope_type, current_value, status) VALUES "
        "('mds_sm22_vm', 'VENDOR_MODE', 'GLOBAL', 'SINGLE', 'STEADY')"
    ))
    yield "mds_sm22_vm"
    session.rollback()


def test_illegal_transition_raises(session, switch):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, switch, "FLIP", PLATFORM_ADMIN)  # can't flip before PROPOSE
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_wrong_actor_forbidden(session, switch):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, switch, "PROPOSE", COMPLIANCE)
    assert exc.value.code == "FORBIDDEN"


def test_full_lifecycle_no_gates_named_passes_trivially(session, switch):
    row = MACHINE.apply(session, switch, "PROPOSE", PLATFORM_ADMIN, proposed_value="CLASSIC")
    assert row["status"] == "PROPOSED" and row["proposed_value"] == "CLASSIC"
    row = MACHINE.apply(session, switch, "CHECK_GATES", "SYSTEM")
    assert row["status"] == "GATE_CHECKING" and row["gate_results"] == []
    row = MACHINE.apply(session, switch, "GATES_PASSED", "SYSTEM")
    assert row["status"] == "READY"
    row = MACHINE.apply(session, switch, "FLIP", PLATFORM_ADMIN)
    assert row["status"] == "FLIPPED"
    assert row["current_value"] == "CLASSIC" and row["previous_value"] == "CONVERSATIONAL"
    assert row["observation_ends_at"] is not None
    audit = session.execute(
        text("SELECT 1 FROM audit_event WHERE action_code='MODE_SWITCH_FLIPPED' AND subject_id=:s"), {"s": switch}
    ).first()
    assert audit is not None

    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, switch, "OBSERVATION_ELAPSED", "SYSTEM")  # window not elapsed yet
    assert exc.value.code == "GUARD_FAILED"

    row = MACHINE.apply(session, switch, "ROLLBACK", PLATFORM_ADMIN)
    assert row["status"] == "ROLLED_BACK" and row["current_value"] == "CONVERSATIONAL"
    row = MACHINE.apply(session, switch, "ROLLBACK_ACK", "SYSTEM")
    assert row["status"] == "STEADY"


def test_flip_guard_fails_without_gate_check(session, switch):
    MACHINE.apply(session, switch, "PROPOSE", PLATFORM_ADMIN, proposed_value="CLASSIC")
    session.execute(text("UPDATE mode_switch SET status='READY' WHERE id=:id"), {"id": switch})
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, switch, "FLIP", PLATFORM_ADMIN)
    assert exc.value.code == "GUARD_FAILED"
    assert exc.value.rule == "R-140"


def test_vendor_mode_gate_g4_fails_with_no_active_vendors(session, vendor_switch):
    # G4 counts ACTIVE vendor_account rows platform-wide (rova/modes/gates.py)
    # — other test files commit their own ACTIVE vendors via db_engine.begin()
    # (not rolled back), so a full-suite run can leave >= 2 already visible to
    # this test's own session. Neutralise them here, inside this session's own
    # transaction (rolled back at teardown, so nothing else is affected), to
    # keep the gate's pass/fail outcome deterministic regardless of run order.
    session.execute(text("UPDATE vendor_account SET status='SUSPENDED', suspension_cause='MANUAL_INCIDENT' "
                          "WHERE status='ACTIVE'"))
    MACHINE.apply(session, vendor_switch, "PROPOSE", PLATFORM_ADMIN, proposed_value="MULTI")
    row = MACHINE.apply(session, vendor_switch, "CHECK_GATES", "SYSTEM")
    assert row["gate_results"][0]["gate_id"] == "G4" and row["gate_results"][0]["passed"] is False
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, vendor_switch, "GATES_PASSED", "SYSTEM")
    assert exc.value.code == "GUARD_FAILED"
    row = MACHINE.apply(session, vendor_switch, "GATES_FAILED", "SYSTEM")
    assert row["status"] == "PROPOSED"  # back to PROPOSED, not READY


def test_no_update_writes_current_value_outside_sm22():
    """R-140 structural check (grepped, not executed): the only writes to
    mode_switch.current_value anywhere in src/ are this module's own FLIP
    (_extra_flip) and ROLLBACK (extra_set) transitions."""
    import pathlib
    import re

    src_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    offenders = []
    for path in src_root.rglob("*.py"):
        if path.name == "sm22_mode_switch.py":
            continue
        text_content = path.read_text()
        if re.search(r"current_value\s*=", text_content):
            offenders.append(str(path))
    assert offenders == []
