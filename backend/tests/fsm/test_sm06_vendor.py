"""A17 'Every state machine' row for SM-06 (§6 SM-06) — vendor_account.
ONBOARDING -> ACTIVE (APPROVE, guarded by R-107/R-111/R-112/R-143) /
REJECTED (REJECT_VERIFICATION) -> ONBOARDING (RESUBMIT, R-127); ACTIVE <->
SUSPENDED (MANUAL_SUSPEND/REINSTATE); ACTIVE <-> LICENCE_EXPIRING
(LICENCE_WARNING/LICENCE_RENEWED/LICENCE_EXPIRED, fired by SM-08); CLOSE
from ACTIVE/SUSPENDED/LICENCE_EXPIRING. One of item 5's 8 missing machine
test files (backend-review-r1.md)."""
import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

from .matrix import assert_all_illegal

MACHINE = MACHINES["SM-06"]

COMPLIANCE = Principal(user_id=None, roles=frozenset({"ComplianceOfficer"}))
PLATFORM_ADMIN = Principal(user_id=None, roles=frozenset({"PlatformAdmin"}))
VENDOR_ADMIN = Principal(user_id=None, roles=frozenset({"VendorAdmin"}))


@pytest.fixture
def vendor(session):
    org_id, ven_id = new_id("org"), new_id("ven")
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:o, :o, 'V6', 'v6', 'VENDOR')"
    ), {"o": org_id})
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount, sourcing_attestation, status) VALUES "
        "(:v, :o, 'MAPUTO_CIDADE', 'V6', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0, true, 'ONBOARDING')"
    ), {"v": ven_id, "o": org_id})
    yield ven_id
    session.rollback()


def _make_approvable(session, vendor):
    session.execute(text(
        "INSERT INTO licence (id, holder_type, holder_id, type, number, issue_date, expiry_date, document_ref, "
        "status) VALUES (:id, 'VENDOR', :v, 'WHOLESALE_ALVARA', 'ALV-6', '2025-01-01', '2099-01-01', 'ref', 'VALID')"
    ), {"id": new_id("lic"), "v": vendor})
    session.execute(text(
        "INSERT INTO vendor_agreement (id, vendor_id, signed_at, document_ref, multi_vendor_clause_ack, status) "
        "VALUES (:id, :v, now(), 'ref', true, 'ACTIVE')"
    ), {"id": new_id("vag"), "v": vendor})


def test_approve_guard_fails_without_alvara_then_passes(session, vendor):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, vendor, "APPROVE", COMPLIANCE)
    assert exc.value.code == "GUARD_FAILED" and exc.value.rule == "R-111"

    _make_approvable(session, vendor)
    row = MACHINE.apply(session, vendor, "APPROVE", COMPLIANCE)
    assert row["status"] == "ACTIVE"


def test_reject_then_resubmit(session, vendor):
    row = MACHINE.apply(session, vendor, "REJECT_VERIFICATION", COMPLIANCE)
    assert row["status"] == "REJECTED"
    row = MACHINE.apply(session, vendor, "RESUBMIT", VENDOR_ADMIN)
    assert row["status"] == "ONBOARDING"


def test_manual_suspend_sets_cause_then_reinstate_clears_it(session, vendor):
    _make_approvable(session, vendor)
    MACHINE.apply(session, vendor, "APPROVE", COMPLIANCE)
    row = MACHINE.apply(session, vendor, "MANUAL_SUSPEND", PLATFORM_ADMIN)
    assert row["status"] == "SUSPENDED" and row["suspension_cause"] == "MANUAL_INCIDENT"
    row = MACHINE.apply(session, vendor, "REINSTATE", PLATFORM_ADMIN)
    assert row["status"] == "ACTIVE" and row["suspension_cause"] is None


def test_licence_expiring_cycle(session, vendor):
    _make_approvable(session, vendor)
    MACHINE.apply(session, vendor, "APPROVE", COMPLIANCE)
    row = MACHINE.apply(session, vendor, "LICENCE_WARNING", "SYSTEM")
    assert row["status"] == "LICENCE_EXPIRING"
    row = MACHINE.apply(session, vendor, "LICENCE_RENEWED", COMPLIANCE)
    assert row["status"] == "ACTIVE"

    MACHINE.apply(session, vendor, "LICENCE_WARNING", "SYSTEM")
    row = MACHINE.apply(session, vendor, "LICENCE_EXPIRED", "SYSTEM")
    assert row["status"] == "SUSPENDED" and row["suspension_cause"] == "LICENCE_EXPIRED"


def test_close_from_active_suspended_or_expiring(session, vendor):
    _make_approvable(session, vendor)
    MACHINE.apply(session, vendor, "APPROVE", COMPLIANCE)
    row = MACHINE.apply(session, vendor, "CLOSE", PLATFORM_ADMIN)
    assert row["status"] == "CLOSED"


def test_wrong_actor_forbidden(session, vendor):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, vendor, "APPROVE", VENDOR_ADMIN)
    assert exc.value.code == "FORBIDDEN"


def test_illegal_transition_matrix(session, vendor):
    assert_all_illegal(
        session, MACHINE, vendor, PLATFORM_ADMIN,
        extra_by_state={"SUSPENDED": {"suspension_cause": "MANUAL_INCIDENT"}},
    )
