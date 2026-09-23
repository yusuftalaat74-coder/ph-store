"""A17 'Every state machine' row for SM-20 (addendum §A7, A4.2 row SM-20) —
eta_estimate.status: COMMIT, NARROW_ON_DISPATCH, GO_LIVE, REALISE/MISS
(the spec's single MATCH-style branch on realised_at vs latest_at, split into
two triggers per A4.1), LATEST_PASSED, VOID. Also exercises `create_provisional`
directly (rova/domain/machines/sm20_eta_estimate.py), the module's public
entry point used by checkout/SM-02 instead of a Transition.effects hook."""
from datetime import timedelta

import pytest
from sqlalchemy import text

from rova.core.clock import now
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain.machines.registry import MACHINES
from rova.domain.machines.sm20_eta_estimate import create_provisional, open_estimate_id

MACHINE = MACHINES["SM-20"]


@pytest.fixture
def order_id(session):
    org_v, org_p = new_id("org"), new_id("org")
    ven_id, pha_id = new_id("ven"), new_id("pha")
    idx_id = new_id("idx")
    req_id, rql_id, ord_id = new_id("req"), new_id("rql"), new_id("ord")
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:v, :v, 'V20', 'v20', 'VENDOR'), (:p, :p, 'P20', 'p20', 'PHARMACY')"
    ), {"v": org_v, "p": org_p})
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES (:id, :o, 'MAPUTO_CIDADE', 'V20', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0)"
    ), {"id": ven_id, "o": org_v})
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES (:id, :o, 'MAPUTO_CIDADE', 'A', 'P20', 'X', 0, 0)"
    ), {"id": pha_id, "o": org_p})
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        "(:id, 'D20', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x20', 'd20')"
    ), {"id": idx_id})
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "(:id, 'RQ-SM20-1', :p, 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ), {"id": req_id, "p": pha_id})
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES (:id, :r, :i, 4)"
    ), {"id": rql_id, "r": req_id, "i": idx_id})
    session.execute(text(
        "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
        "delivery_mode, goods_total) VALUES (:id, 'RQ-SM20-1-V01', :r, :v, :p, 'PENDING_ACCEPTANCE', 'UPFRONT', "
        "'VENDOR_OWN_FLEET', 80)"
    ), {"id": ord_id, "r": req_id, "v": ven_id, "p": pha_id})
    yield ord_id
    session.rollback()


def test_create_provisional_uses_config_not_hardcoded(session, order_id):
    eta_id = create_provisional(session, order_id)
    row = session.execute(text("SELECT * FROM eta_estimate WHERE id=:id"), {"id": eta_id}).mappings().one()
    assert row["status"] == "PROVISIONAL" and row["basis"] == "SLA_BOUNDS"
    assert row["latest_at"] > row["earliest_at"]
    assert open_estimate_id(session, order_id) == eta_id


def test_illegal_transition_raises(session, order_id):
    eta_id = create_provisional(session, order_id)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, eta_id, "GO_LIVE", "SYSTEM")  # can't go live before COMMIT
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_full_lifecycle_commit_go_live_realise(session, order_id):
    eta_id = create_provisional(session, order_id)
    row = MACHINE.apply(session, eta_id, "COMMIT", "SYSTEM")
    assert row["status"] == "COMMITTED"
    row = MACHINE.apply(session, eta_id, "NARROW_ON_DISPATCH", "SYSTEM")
    assert row["status"] == "COMMITTED" and row["order_state"] == "DISPATCHED"
    row = MACHINE.apply(session, eta_id, "GO_LIVE", "SYSTEM")
    assert row["status"] == "IN_TRANSIT_LIVE"
    row = MACHINE.apply(session, eta_id, "REALISE", "SYSTEM", realised_at=now())
    assert row["status"] == "REALISED" and row["error_minutes"] == 0


def test_miss_when_realised_after_latest(session, order_id):
    eta_id = create_provisional(session, order_id)
    MACHINE.apply(session, eta_id, "COMMIT", "SYSTEM")
    latest_at = session.execute(text("SELECT latest_at FROM eta_estimate WHERE id=:id"), {"id": eta_id}).scalar()
    late = latest_at + timedelta(hours=2)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, eta_id, "REALISE", "SYSTEM", realised_at=late)
    assert exc.value.code == "GUARD_FAILED"
    row = MACHINE.apply(session, eta_id, "MISS", "SYSTEM", realised_at=late)
    assert row["status"] == "MISSED" and row["error_minutes"] == 120


def test_latest_passed_recreates_provisional_estimate(session, order_id):
    eta_id = create_provisional(session, order_id)
    MACHINE.apply(session, eta_id, "COMMIT", "SYSTEM")
    row = MACHINE.apply(session, eta_id, "LATEST_PASSED", "SYSTEM")
    assert row["status"] == "MISSED"
    new_eta_id = open_estimate_id(session, order_id)
    assert new_eta_id is not None and new_eta_id != eta_id
    new_row = session.execute(text("SELECT status FROM eta_estimate WHERE id=:id"), {"id": new_eta_id}).mappings().one()
    assert new_row["status"] == "PROVISIONAL"


def test_void_from_any_open_state(session, order_id):
    eta_id = create_provisional(session, order_id)
    row = MACHINE.apply(session, eta_id, "VOID", "SYSTEM")
    assert row["status"] == "VOID"
