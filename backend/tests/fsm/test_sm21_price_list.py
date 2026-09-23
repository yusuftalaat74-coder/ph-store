"""A17 'Every state machine' row for SM-21 (§6 SM-21, A12) —
price_list_version. UPLOADED -> {VALIDATING, MAPPING_NEEDED} -> VALIDATING
-> {VALIDATED, REJECTED} -> {SCHEDULED, REJECTED} -> {LIVE, REJECTED} ->
SUPERSEDED. This module only fixes the legal state graph and who may drive
it (the pipeline itself lives in rova/pricelist/) — same split this test
file exercises: legal-graph correctness, not the parser/validator. One of
item 5's 8 missing machine test files (backend-review-r1.md)."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

from .matrix import assert_all_illegal

MACHINE = MACHINES["SM-21"]

VENDOR_ADMIN = Principal(user_id=None, roles=frozenset({"VendorAdmin"}))
VENDOR_ORDER_DESK = Principal(user_id=None, roles=frozenset({"VendorOrderDesk"}))
OPS_REVIEWER = Principal(user_id=None, roles=frozenset({"OpsReviewer"}))
PHARMACY_ADMIN = Principal(user_id=None, roles=frozenset({"PharmacyAdmin"}))


@pytest.fixture
def version(session):
    org_id, ven_id, plv_id = new_id("org"), new_id("ven"), new_id("plv")
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:o, :o, 'V21', 'v21', 'VENDOR')"
    ), {"o": org_id})
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES (:v, :o, 'MAPUTO_CIDADE', 'V21', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0)"
    ), {"v": ven_id, "o": org_id})
    usr_id = new_id("usr")
    session.execute(text(
        "INSERT INTO app_user (id, phone, name, password_hash) VALUES (:u, :ph, 'U21', 'x')"
    ), {"u": usr_id, "ph": f"+2588400{usr_id[-6:]}"})
    session.execute(text(
        "INSERT INTO price_list_version (id, vendor_id, version_number, source_file_ref, source_file_name, "
        "fingerprint, uploaded_by_user_id, uploaded_at, partial_update, qty_only, row_count, accepted_rows, "
        "rejected_rows, warning_rows, pending_rows) VALUES "
        "(:id, :v, 1, 'ref', 'file.csv', 'fp21', :u, now(), false, false, 0, 0, 0, 0, 0)"
    ), {"id": plv_id, "v": ven_id, "u": usr_id})
    yield plv_id
    session.rollback()


def test_mapping_needed_path_then_validate_then_publish_then_live(session, version):
    row = MACHINE.apply(session, version, "MAPPING_NEEDED", "SYSTEM")
    assert row["status"] == "MAPPING_NEEDED"
    row = MACHINE.apply(session, version, "MAPPING_SAVED", VENDOR_ADMIN)
    assert row["status"] == "VALIDATING"
    row = MACHINE.apply(session, version, "VALIDATED", "SYSTEM")
    assert row["status"] == "VALIDATED"
    row = MACHINE.apply(session, version, "PUBLISH", VENDOR_ORDER_DESK, effective_from=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert row["status"] == "SCHEDULED"
    row = MACHINE.apply(session, version, "GO_LIVE", "SYSTEM")
    assert row["status"] == "LIVE" and row["went_live_at"] is not None
    row = MACHINE.apply(session, version, "SUPERSEDE", "SYSTEM")
    assert row["status"] == "SUPERSEDED"


def test_mapping_found_path_then_all_rejected(session, version):
    row = MACHINE.apply(session, version, "MAPPING_FOUND", "SYSTEM")
    assert row["status"] == "VALIDATING"
    row = MACHINE.apply(session, version, "ALL_REJECTED", "SYSTEM")
    assert row["status"] == "REJECTED"


def test_abandon_from_mapping_needed(session, version):
    MACHINE.apply(session, version, "MAPPING_NEEDED", "SYSTEM")
    row = MACHINE.apply(session, version, "ABANDON", OPS_REVIEWER)
    assert row["status"] == "REJECTED"


def test_discard_validated_and_cancel_scheduled(session, version):
    MACHINE.apply(session, version, "MAPPING_FOUND", "SYSTEM")
    MACHINE.apply(session, version, "VALIDATED", "SYSTEM")
    row = MACHINE.apply(session, version, "DISCARD", VENDOR_ADMIN)
    assert row["status"] == "REJECTED"


def test_cancel_scheduled(session, version):
    MACHINE.apply(session, version, "MAPPING_FOUND", "SYSTEM")
    MACHINE.apply(session, version, "VALIDATED", "SYSTEM")
    MACHINE.apply(session, version, "PUBLISH", VENDOR_ORDER_DESK, effective_from=datetime(2026, 6, 1, tzinfo=timezone.utc))
    row = MACHINE.apply(session, version, "CANCEL_SCHEDULED", VENDOR_ADMIN)
    assert row["status"] == "REJECTED"


def test_wrong_actor_forbidden(session, version):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, version, "MAPPING_FOUND", VENDOR_ADMIN)  # SYSTEM only
    assert exc.value.code == "FORBIDDEN"
    MACHINE.apply(session, version, "MAPPING_NEEDED", "SYSTEM")
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, version, "MAPPING_SAVED", VENDOR_ORDER_DESK)  # VendorAdmin/OpsReviewer only
    assert exc.value.code == "FORBIDDEN"


def test_illegal_transition_matrix(session, version):
    assert_all_illegal(session, MACHINE, version, VENDOR_ADMIN)
