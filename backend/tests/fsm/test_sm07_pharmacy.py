"""A17 'Every state machine' row for SM-07 (§6 SM-07) — pharmacy_account.
Mirrors SM-06's shape (ONBOARDING -> ACTIVE/REJECTED -> ONBOARDING; ACTIVE
<-> SUSPENDED; ACTIVE <-> LICENCE_EXPIRING; CLOSE). One of item 5's 8
missing machine test files (backend-review-r1.md)."""
import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

from .matrix import assert_all_illegal

MACHINE = MACHINES["SM-07"]

COMPLIANCE = Principal(user_id=None, roles=frozenset({"ComplianceOfficer"}))
PLATFORM_ADMIN = Principal(user_id=None, roles=frozenset({"PlatformAdmin"}))
PHARMACY_ADMIN = Principal(user_id=None, roles=frozenset({"PharmacyAdmin"}))


@pytest.fixture
def pharmacy(session):
    org_id, pha_id = new_id("org"), new_id("pha")
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:o, :o, 'P7', 'p7', 'PHARMACY')"
    ), {"o": org_id})
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude, status) VALUES (:p, :o, 'MAPUTO_CIDADE', 'A', 'P7', 'X', 0, 0, 'ONBOARDING')"
    ), {"p": pha_id, "o": org_id})
    yield pha_id
    session.rollback()


def _make_approvable(session, pharmacy):
    session.execute(text(
        "INSERT INTO licence (id, holder_type, holder_id, type, number, issue_date, expiry_date, document_ref, "
        "status) VALUES (:id, 'PHARMACY', :p, 'RETAIL_A', 'RET-7', '2025-01-01', '2099-01-01', 'ref', 'VALID')"
    ), {"id": new_id("lic"), "p": pharmacy})


def test_approve_guard_fails_without_licence_then_passes(session, pharmacy):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, pharmacy, "APPROVE", COMPLIANCE)
    assert exc.value.code == "GUARD_FAILED" and exc.value.rule == "R-113"

    _make_approvable(session, pharmacy)
    row = MACHINE.apply(session, pharmacy, "APPROVE", COMPLIANCE)
    assert row["status"] == "ACTIVE"


def test_reject_then_resubmit(session, pharmacy):
    row = MACHINE.apply(session, pharmacy, "REJECT_VERIFICATION", COMPLIANCE)
    assert row["status"] == "REJECTED"
    row = MACHINE.apply(session, pharmacy, "RESUBMIT", PHARMACY_ADMIN)
    assert row["status"] == "ONBOARDING"


def test_manual_suspend_sets_cause_then_reinstate_clears_it(session, pharmacy):
    _make_approvable(session, pharmacy)
    MACHINE.apply(session, pharmacy, "APPROVE", COMPLIANCE)
    row = MACHINE.apply(session, pharmacy, "MANUAL_SUSPEND", PLATFORM_ADMIN)
    assert row["status"] == "SUSPENDED" and row["suspension_cause"] == "MANUAL_INCIDENT"
    row = MACHINE.apply(session, pharmacy, "REINSTATE", PLATFORM_ADMIN)
    assert row["status"] == "ACTIVE" and row["suspension_cause"] is None


def test_licence_expiring_cycle(session, pharmacy):
    _make_approvable(session, pharmacy)
    MACHINE.apply(session, pharmacy, "APPROVE", COMPLIANCE)
    row = MACHINE.apply(session, pharmacy, "LICENCE_WARNING", "SYSTEM")
    assert row["status"] == "LICENCE_EXPIRING"
    row = MACHINE.apply(session, pharmacy, "LICENCE_RENEWED", COMPLIANCE)
    assert row["status"] == "ACTIVE"

    MACHINE.apply(session, pharmacy, "LICENCE_WARNING", "SYSTEM")
    row = MACHINE.apply(session, pharmacy, "LICENCE_EXPIRED", "SYSTEM")
    assert row["status"] == "SUSPENDED" and row["suspension_cause"] == "LICENCE_EXPIRED"


def test_close_from_active(session, pharmacy):
    _make_approvable(session, pharmacy)
    MACHINE.apply(session, pharmacy, "APPROVE", COMPLIANCE)
    row = MACHINE.apply(session, pharmacy, "CLOSE", PLATFORM_ADMIN)
    assert row["status"] == "CLOSED"


def test_wrong_actor_forbidden(session, pharmacy):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, pharmacy, "APPROVE", PHARMACY_ADMIN)
    assert exc.value.code == "FORBIDDEN"


def test_illegal_transition_matrix(session, pharmacy):
    assert_all_illegal(
        session, MACHINE, pharmacy, PLATFORM_ADMIN,
        extra_by_state={"SUSPENDED": {"suspension_cause": "MANUAL_INCIDENT"}},
    )
