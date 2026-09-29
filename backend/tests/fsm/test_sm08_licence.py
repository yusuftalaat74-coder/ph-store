"""A17 'Every state machine' row for SM-08 (§6 SM-08) — licence.status:
OPEN_REVIEW, APPROVE, REJECT, ENTER_WARNING_WINDOW, RENEW, EXPIRE, and the
holder-cascade effects onto SM-07 (pharmacy_account) — APPROVE/LICENCE_
WARNING/LICENCE_RENEWED/LICENCE_EXPIRED fired only when the holder is
currently in the state each of those transitions expects."""
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

MACHINE = MACHINES["SM-08"]

COMPLIANCE = Principal(user_id=None, roles=frozenset({"ComplianceOfficer"}))
PHARMACY_ADMIN = Principal(user_id=None, roles=frozenset({"PharmacyAdmin"}))


@pytest.fixture
def pharmacy(session):
    org_id = new_id("org")
    pha_id = new_id("pha")
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:o, :o, 'P8', 'p8', 'PHARMACY')"
    ), {"o": org_id})
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude, status) VALUES (:p, :o, 'MAPUTO_CIDADE', 'A', 'P8', 'X', 0, 0, 'ONBOARDING')"
    ), {"p": pha_id, "o": org_id})
    yield pha_id
    session.rollback()


@pytest.fixture
def licence(session, pharmacy):
    lic_id = new_id("lic")
    session.execute(text(
        "INSERT INTO licence (id, holder_type, holder_id, type, number, issue_date, expiry_date, document_ref, "
        "status) VALUES (:id, 'PHARMACY', :h, 'RETAIL_A', 'LIC-1', :issue, :expiry, 'ref', 'SUBMITTED')"
    ), {"id": lic_id, "h": pharmacy, "issue": date(2020, 1, 1), "expiry": date(2099, 1, 1)})
    yield lic_id
    session.rollback()


def test_open_review_then_approve_activates_holder(session, licence, pharmacy):
    row = MACHINE.apply(session, licence, "OPEN_REVIEW", COMPLIANCE)
    assert row["status"] == "UNDER_REVIEW"
    row = MACHINE.apply(session, licence, "APPROVE", COMPLIANCE)
    assert row["status"] == "VALID"
    holder_status = session.execute(text("SELECT status FROM pharmacy_account WHERE id=:p"), {"p": pharmacy}).scalar()
    assert holder_status == "ACTIVE"


def test_illegal_transition_raises(session, licence):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, licence, "APPROVE", COMPLIANCE)  # can't approve before OPEN_REVIEW
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_wrong_actor_forbidden(session, licence):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, licence, "OPEN_REVIEW", PHARMACY_ADMIN)
    assert exc.value.code == "FORBIDDEN"


def test_reject_terminal(session, licence):
    MACHINE.apply(session, licence, "OPEN_REVIEW", COMPLIANCE)
    row = MACHINE.apply(session, licence, "REJECT", COMPLIANCE)
    assert row["status"] == "REJECTED"


def _activate(session, licence, pharmacy):
    MACHINE.apply(session, licence, "OPEN_REVIEW", COMPLIANCE)
    MACHINE.apply(session, licence, "APPROVE", COMPLIANCE)


def test_enter_warning_window_guard_and_cascade(session, licence, pharmacy):
    _activate(session, licence, pharmacy)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, licence, "ENTER_WARNING_WINDOW", "SYSTEM")  # expiry_date far in the future
    assert exc.value.code == "GUARD_FAILED"

    session.execute(text("UPDATE licence SET expiry_date=:e WHERE id=:id"),
                     {"e": date.today() + timedelta(days=10), "id": licence})
    row = MACHINE.apply(session, licence, "ENTER_WARNING_WINDOW", "SYSTEM")
    assert row["status"] == "EXPIRING_SOON"
    holder_status = session.execute(text("SELECT status FROM pharmacy_account WHERE id=:p"), {"p": pharmacy}).scalar()
    assert holder_status == "LICENCE_EXPIRING"


def test_renew_requires_valid_successor_then_cascades(session, licence, pharmacy):
    _activate(session, licence, pharmacy)
    session.execute(text("UPDATE licence SET expiry_date=:e WHERE id=:id"),
                     {"e": date.today() + timedelta(days=1), "id": licence})
    MACHINE.apply(session, licence, "ENTER_WARNING_WINDOW", "SYSTEM")

    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, licence, "RENEW", COMPLIANCE)  # no successor_id
    assert exc.value.code == "GUARD_FAILED"

    successor_id = new_id("lic")
    session.execute(text(
        "INSERT INTO licence (id, holder_type, holder_id, type, number, issue_date, expiry_date, document_ref, "
        "status) VALUES (:id, 'PHARMACY', :h, 'RETAIL_A', 'LIC-2', :issue, :expiry, 'ref', 'SUBMITTED')"
    ), {"id": successor_id, "h": pharmacy, "issue": date.today(), "expiry": date(2099, 1, 1)})
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, licence, "RENEW", COMPLIANCE, successor_id=successor_id)  # successor not VALID yet
    assert exc.value.code == "GUARD_FAILED"

    session.execute(text("UPDATE licence SET status='VALID' WHERE id=:id"), {"id": successor_id})
    row = MACHINE.apply(session, licence, "RENEW", COMPLIANCE, successor_id=successor_id)
    assert row["status"] == "RENEWED" and row["successor_id"] == successor_id
    holder_status = session.execute(text("SELECT status FROM pharmacy_account WHERE id=:p"), {"p": pharmacy}).scalar()
    assert holder_status == "ACTIVE"


def test_expire_guard_and_cascade_to_suspended(session, licence, pharmacy):
    _activate(session, licence, pharmacy)
    session.execute(text("UPDATE licence SET expiry_date=:e WHERE id=:id"),
                     {"e": date.today() + timedelta(days=1), "id": licence})
    MACHINE.apply(session, licence, "ENTER_WARNING_WINDOW", "SYSTEM")

    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, licence, "EXPIRE", "SYSTEM")  # expiry_date has not passed yet
    assert exc.value.code == "GUARD_FAILED"

    session.execute(text("UPDATE licence SET expiry_date=:e WHERE id=:id"),
                     {"e": date.today() - timedelta(days=1), "id": licence})
    row = MACHINE.apply(session, licence, "EXPIRE", "SYSTEM")
    assert row["status"] == "EXPIRED"
    holder = session.execute(
        text("SELECT status, suspension_cause FROM pharmacy_account WHERE id=:p"), {"p": pharmacy}
    ).mappings().one()
    assert holder["status"] == "SUSPENDED" and holder["suspension_cause"] == "LICENCE_EXPIRED"


@pytest.mark.parametrize("role", ["PlatformAdmin", "OpsReviewer"])
def test_review_team_can_open_review_approve_and_reject(session, pharmacy, role):
    """Signup spec D-6: OPEN_REVIEW / APPROVE / REJECT accept the whole review
    team, and APPROVE still drives the holder through SM-07 with that actor."""
    reviewer = Principal(user_id=None, roles=frozenset({role}))
    ids = []
    for _ in range(2):
        lic_id = new_id("lic")
        session.execute(text(
            "INSERT INTO licence (id, holder_type, holder_id, type, number, issue_date, expiry_date, "
            "document_ref, status) VALUES (:id, 'PHARMACY', :h, 'RETAIL_A', 'LIC-R', '2020-01-01', "
            "'2099-01-01', 'ref', 'SUBMITTED')"), {"id": lic_id, "h": pharmacy})
        ids.append(lic_id)
    assert MACHINE.apply(session, ids[0], "OPEN_REVIEW", reviewer)["status"] == "UNDER_REVIEW"
    assert MACHINE.apply(session, ids[0], "REJECT", reviewer)["status"] == "REJECTED"
    MACHINE.apply(session, ids[1], "OPEN_REVIEW", reviewer)
    assert MACHINE.apply(session, ids[1], "APPROVE", reviewer)["status"] == "VALID"
    assert session.execute(text("SELECT status FROM pharmacy_account WHERE id=:p"),
                           {"p": pharmacy}).scalar() == "ACTIVE"


def test_a_valid_licence_must_carry_number_and_dates(session, pharmacy):
    """0009: number/dates are nullable while the licence is waiting (the
    reviewer types them), and a CHECK refuses VALID without them."""
    from sqlalchemy.exc import IntegrityError
    lic_id = new_id("lic")
    session.execute(text(
        "INSERT INTO licence (id, holder_type, holder_id, type, status) "
        "VALUES (:id, 'PHARMACY', :h, 'RETAIL_A', 'SUBMITTED')"), {"id": lic_id, "h": pharmacy})
    MACHINE.apply(session, lic_id, "OPEN_REVIEW", COMPLIANCE)
    with pytest.raises(IntegrityError):
        MACHINE.apply(session, lic_id, "APPROVE", COMPLIANCE)
