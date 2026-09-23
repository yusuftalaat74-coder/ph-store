"""A17 'Every state machine' row for SM-12 (§6 SM-12) — vendor_offer.
freshness_state: GO_STALE, RECONFIRM (a self-loop that also refreshes
stock_confirmed_at, R-019), WITHDRAW, QTY_ZERO (R-018). One of item 5's 8
missing machine test files (backend-review-r1.md)."""
import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

from .matrix import assert_all_illegal

MACHINE = MACHINES["SM-12"]

VENDOR_ADMIN = Principal(user_id=None, roles=frozenset({"VendorAdmin"}))
COURIER = Principal(user_id=None, roles=frozenset({"Courier"}))


@pytest.fixture
def offer(session):
    org_id, ven_id, idx_id, ofr_id = new_id("org"), new_id("ven"), new_id("idx"), new_id("ofr")
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:o, :o, 'V12', 'v12', 'VENDOR')"
    ), {"o": org_id})
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES (:v, :o, 'MAPUTO_CIDADE', 'V12', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0)"
    ), {"v": ven_id, "o": org_id})
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        "(:p, 'D12', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x12', 'd12')"
    ), {"p": idx_id})
    session.execute(text(
        "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, pack_size, "
        "expiry_horizon_days, price, stock_confirmed_at, freshness_state) VALUES "
        "(:id, :v, :p, false, 50, '1', 300, 10.00, now(), 'FRESH')"
    ), {"id": ofr_id, "v": ven_id, "p": idx_id})
    yield ofr_id
    session.rollback()


def test_go_stale_then_reconfirm_refreshes_stock_confirmed_at(session, offer):
    row = MACHINE.apply(session, offer, "GO_STALE", "SYSTEM")
    assert row["freshness_state"] == "STALE"
    stale_stock_ts = row["stock_confirmed_at"]

    row = MACHINE.apply(session, offer, "RECONFIRM", VENDOR_ADMIN)
    assert row["freshness_state"] == "FRESH"
    assert row["stock_confirmed_at"] > stale_stock_ts  # R-019: RECONFIRM refreshes the timestamp


def test_withdraw_from_fresh_or_stale(session, offer):
    row = MACHINE.apply(session, offer, "WITHDRAW", VENDOR_ADMIN)
    assert row["freshness_state"] == "WITHDRAWN"


def test_qty_zero_by_system(session, offer):
    row = MACHINE.apply(session, offer, "QTY_ZERO", "SYSTEM")
    assert row["freshness_state"] == "WITHDRAWN"


def test_wrong_actor_forbidden(session, offer):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, offer, "RECONFIRM", COURIER)
    assert exc.value.code == "FORBIDDEN"


def test_withdrawn_is_terminal_no_exit(session, offer):
    MACHINE.apply(session, offer, "WITHDRAW", VENDOR_ADMIN)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, offer, "RECONFIRM", VENDOR_ADMIN)
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_illegal_transition_matrix(session, offer):
    """Every (freshness_state, trigger) pair NOT in MACHINE.transitions,
    generated from the machine itself (item 5) — not hand-picked."""
    assert_all_illegal(session, MACHINE, offer, VENDOR_ADMIN)
