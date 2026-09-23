"""A17 'Every state machine' row for SM-23 (addendum §A10, R-167-R-169) —
pms_link.status: ACCEPT_TERMS (guarded by pharmacy ACTIVE + a PMS_LINK_TERMS
terms_version with mentions_pms_aggregation, R-161), DECLINE, SUSPEND,
RESUME, REVOKE."""
import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

MACHINE = MACHINES["SM-23"]

PHARMACY_ADMIN = Principal(user_id="usr_sm23", roles=frozenset({"PharmacyAdmin"}))
BUYER = Principal(user_id=None, roles=frozenset({"PharmacyBuyer"}))


@pytest.fixture
def link(session):
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "('org_sm23', '923', 'P23', 'p23', 'PHARMACY')"
    ))
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude, status) VALUES ('pha_sm23', 'org_sm23', 'MAPUTO_CIDADE', 'A', 'P23', 'X', 0, 0, "
        "'ACTIVE')"
    ))
    session.execute(text(
        "INSERT INTO app_user (id, phone, name, password_hash) VALUES ('usr_sm23', '+258001', 'U', 'x')"
    ))
    session.execute(text(
        "INSERT INTO pms_link (id, pharmacy_id, pms_tenant_id, api_key_hash, status) VALUES "
        "('pml_sm23', 'pha_sm23', 'tenant1', 'hash1', 'PENDING_CONSENT')"
    ))
    yield "pml_sm23"
    session.rollback()


def test_illegal_transition_raises(session, link):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, link, "SUSPEND", "SYSTEM")  # can't suspend before LINKED
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_wrong_actor_forbidden(session, link):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, link, "ACCEPT_TERMS", BUYER)
    assert exc.value.code == "FORBIDDEN"


def test_accept_terms_guard_requires_pms_link_terms(session, link):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, link, "ACCEPT_TERMS", PHARMACY_ADMIN)  # no terms_version_id
    assert exc.value.code == "GUARD_FAILED"

    # `trm_pharmacy_v1` is a pre-seeded PHARMACY_TERMS row (seed_config_only) —
    # wrong code for a PMS link's consent, must fail R-161. terms_version's
    # (code, version) is genuinely UNIQUE (both NOT NULL, unlike mode_switch's
    # scope_id), so tests reuse the seeded rows rather than inserting new
    # ones with the same natural key.
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, link, "ACCEPT_TERMS", PHARMACY_ADMIN, terms_version_id="trm_pharmacy_v1")
    assert exc.value.code == "GUARD_FAILED"
    assert exc.value.rule == "R-161"


def test_accept_terms_creates_consent_and_links(session, link):
    row = MACHINE.apply(session, link, "ACCEPT_TERMS", PHARMACY_ADMIN, terms_version_id="trm_pms_v1")
    assert row["status"] == "LINKED" and row["linked_at"] is not None
    assert row["consent_record_id"] is not None
    consent = session.execute(
        text("SELECT purpose, subject_type FROM consent_record WHERE id=:c"), {"c": row["consent_record_id"]}
    ).mappings().one()
    assert consent["purpose"] == "PMS_LINK" and consent["subject_type"] == "PHARMACY_ACCOUNT"


def test_suspend_resume_revoke_cycle(session, link):
    MACHINE.apply(session, link, "ACCEPT_TERMS", PHARMACY_ADMIN, terms_version_id="trm_pms_v1")
    row = MACHINE.apply(session, link, "SUSPEND", "SYSTEM")
    assert row["status"] == "SUSPENDED"
    row = MACHINE.apply(session, link, "RESUME", "SYSTEM")
    assert row["status"] == "LINKED"
    row = MACHINE.apply(session, link, "REVOKE", PHARMACY_ADMIN)
    assert row["status"] == "REVOKED" and row["revoked_at"] is not None
    audit = session.execute(
        text("SELECT 1 FROM audit_event WHERE action_code='CONSENT_WITHDRAWN' AND subject_id=:l"), {"l": link}
    ).first()
    assert audit is not None


def test_decline_from_pending_consent(session, link):
    row = MACHINE.apply(session, link, "DECLINE", PHARMACY_ADMIN)
    assert row["status"] == "REVOKED"
