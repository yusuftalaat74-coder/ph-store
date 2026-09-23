"""A17 'Every state machine' row for SM-01 (§6 SM-01) — request. Item 6
(backend-review-r1.md): exercises the transitions that make
AWAITING_ADMIN_APPROVAL/NORMALIZING/AWAITING_CONFIRMATION reachable-with-
exit now (see rova/domain/machines/sm01_request.py's own docstring for why
the previous 4-transition subset left them dead), alongside the
already-implemented catalogue-checkout tail. One of item 5's 8 missing
machine test files (backend-review-r1.md)."""
from decimal import Decimal

import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain import timers
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

from .matrix import assert_all_illegal

MACHINE = MACHINES["SM-01"]
POLICY_ADMIN_APPROVAL = "ADMIN_APPROVAL"
POLICY_CONFIRMATION = "CONFIRMATION"

BUYER = Principal(user_id=None, roles=frozenset({"PharmacyBuyer"}))
ADMIN = Principal(user_id=None, roles=frozenset({"PharmacyAdmin"}))
OPS = Principal(user_id=None, roles=frozenset({"OpsReviewer"}))


@pytest.fixture
def pharmacy(session):
    org_id, pha_id = new_id("org"), new_id("pha")
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:o, :o, 'P1', 'p1', 'PHARMACY')"
    ), {"o": org_id})
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude, status) VALUES (:p, :o, 'MAPUTO_CIDADE', 'A', 'P1', 'X', 0, 0, 'ACTIVE')"
    ), {"p": pha_id, "o": org_id})
    return pha_id


@pytest.fixture
def priced_product(session):
    """A non-regulated product with one FRESH offer, so SM-01's own R-122
    cart-total guard (which re-ranks like checkout.py does) has a real price
    to work with."""
    org_id, ven_id, idx_id = new_id("org"), new_id("ven"), new_id("idx")
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:o, :o, 'V1', 'v1', 'VENDOR')"
    ), {"o": org_id})
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount, status) VALUES (:v, :o, 'MAPUTO_CIDADE', 'V1', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', "
        "0, 'ACTIVE')"
    ), {"v": ven_id, "o": org_id})
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        "(:p, 'D1', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x1', 'd1')"
    ), {"p": idx_id})
    session.execute(text(
        "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, pack_size, "
        "expiry_horizon_days, price, stock_confirmed_at, freshness_state) VALUES "
        "(:id, :v, :p, false, 100, '1', 300, 40.00, now(), 'FRESH')"
    ), {"id": new_id("ofr"), "v": ven_id, "p": idx_id})
    return idx_id


def _request(session, pharmacy, *, status="DRAFT"):
    rid = new_id("req")
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "(:id, :num, :p, 'CATALOGUE', 'APP', :s, 'FEWEST_VENDORS')"
    ), {"id": rid, "num": f"RQ-{rid}", "p": pharmacy, "s": status})
    return rid


def _add_line(session, request_id, product_id, qty=2):
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES (:id, :r, :p, :q)"
    ), {"id": new_id("rql"), "r": request_id, "p": product_id, "q": qty})


def test_submit_cart_no_threshold_set_goes_straight_to_confirmed(session, pharmacy, priced_product):
    rid = _request(session, pharmacy)
    _add_line(session, rid, priced_product)
    row = MACHINE.apply(session, rid, "SUBMIT_CART", BUYER)
    assert row["status"] == "CONFIRMED" and row["confirmed_at"] is not None


def test_submit_cart_guard_fails_on_empty_cart(session, pharmacy):
    rid = _request(session, pharmacy)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, rid, "SUBMIT_CART", BUYER)
    assert exc.value.code == "GUARD_FAILED" and exc.value.rule == "R-030"


def test_submit_cart_within_threshold(session, pharmacy, priced_product):
    session.execute(text("UPDATE pharmacy_account SET buyer_approval_threshold=200 WHERE id=:p"), {"p": pharmacy})
    rid = _request(session, pharmacy)
    _add_line(session, rid, priced_product, qty=2)  # 2 * 40.00 = 80 <= 200
    row = MACHINE.apply(session, rid, "SUBMIT_CART", BUYER)
    assert row["status"] == "CONFIRMED"


def test_submit_cart_over_threshold_then_admin_approve(session, pharmacy, priced_product):
    session.execute(text("UPDATE pharmacy_account SET buyer_approval_threshold=50 WHERE id=:p"), {"p": pharmacy})
    rid = _request(session, pharmacy)
    _add_line(session, rid, priced_product, qty=3)  # 3 * 40.00 = 120 > 50

    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, rid, "SUBMIT_CART", BUYER)  # over threshold: SUBMIT_CART itself must refuse
    assert exc.value.code == "GUARD_FAILED" and exc.value.rule == "R-122"

    row = MACHINE.apply(session, rid, "SUBMIT_CART_OVER_THRESHOLD", BUYER)
    assert row["status"] == "AWAITING_ADMIN_APPROVAL"
    assert timers.is_running(session, policy_type=POLICY_ADMIN_APPROVAL, subject_type="request", subject_id=rid)

    row = MACHINE.apply(session, rid, "ADMIN_APPROVE", ADMIN)
    assert row["status"] == "CONFIRMED" and row["confirmed_at"] is not None
    assert not timers.is_running(session, policy_type=POLICY_ADMIN_APPROVAL, subject_type="request", subject_id=rid)


def test_admin_reject_returns_to_draft(session, pharmacy, priced_product):
    session.execute(text("UPDATE pharmacy_account SET buyer_approval_threshold=50 WHERE id=:p"), {"p": pharmacy})
    rid = _request(session, pharmacy)
    _add_line(session, rid, priced_product, qty=3)
    MACHINE.apply(session, rid, "SUBMIT_CART_OVER_THRESHOLD", BUYER)
    row = MACHINE.apply(session, rid, "ADMIN_REJECT", ADMIN)
    assert row["status"] == "DRAFT"


def test_admin_reject_also_fires_by_system_on_timer_expiry(session, pharmacy, priced_product):
    session.execute(text("UPDATE pharmacy_account SET buyer_approval_threshold=50 WHERE id=:p"), {"p": pharmacy})
    rid = _request(session, pharmacy)
    _add_line(session, rid, priced_product, qty=3)
    MACHINE.apply(session, rid, "SUBMIT_CART_OVER_THRESHOLD", BUYER)
    row = MACHINE.apply(session, rid, "ADMIN_REJECT", "SYSTEM")  # A-142: SlaTimer(admin_approval) expiry, unanswered
    assert row["status"] == "DRAFT"


def test_normalizing_confirmation_cycle(session, pharmacy):
    rid = _request(session, pharmacy, status="NORMALIZING")
    row = MACHINE.apply(session, rid, "NORMALIZATION_COMPLETE", "SYSTEM")
    assert row["status"] == "AWAITING_CONFIRMATION"
    assert timers.is_running(session, policy_type=POLICY_CONFIRMATION, subject_type="request", subject_id=rid)

    row = MACHINE.apply(session, rid, "REQUEST_CHANGES", OPS)  # OpsReviewer acting for AssistedPharmacy, R-115
    assert row["status"] == "NORMALIZING"
    assert not timers.is_running(session, policy_type=POLICY_CONFIRMATION, subject_type="request", subject_id=rid)

    MACHINE.apply(session, rid, "NORMALIZATION_COMPLETE", "SYSTEM")
    row = MACHINE.apply(session, rid, "CONFIRM", OPS)
    assert row["status"] == "CONFIRMED" and row["confirmed_at"] is not None


def test_confirmation_timeout_cancels(session, pharmacy):
    rid = _request(session, pharmacy, status="NORMALIZING")
    MACHINE.apply(session, rid, "NORMALIZATION_COMPLETE", "SYSTEM")
    row = MACHINE.apply(session, rid, "CONFIRMATION_TIMEOUT", "SYSTEM")
    assert row["status"] == "CANCELLED"


def test_confirm_guard_fails_once_timer_cancelled(session, pharmacy):
    rid = _request(session, pharmacy, status="NORMALIZING")
    MACHINE.apply(session, rid, "NORMALIZATION_COMPLETE", "SYSTEM")
    timers.cancel(session, policy_type=POLICY_CONFIRMATION, subject_type="request", subject_id=rid)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, rid, "CONFIRM", OPS)
    assert exc.value.code == "GUARD_FAILED" and exc.value.rule == "A-11"


def test_abandon_draft(session, pharmacy):
    rid = _request(session, pharmacy)
    row = MACHINE.apply(session, rid, "ABANDON", BUYER)
    assert row["status"] == "CANCELLED"


def test_cancel_confirmed_before_any_order_accepted(session, pharmacy, priced_product):
    rid = _request(session, pharmacy)
    _add_line(session, rid, priced_product)
    MACHINE.apply(session, rid, "SUBMIT_CART", BUYER)
    row = MACHINE.apply(session, rid, "CANCEL", ADMIN)
    assert row["status"] == "CANCELLED"


def test_cancel_guard_blocked_once_an_order_is_accepted(session, pharmacy, priced_product):
    rid = _request(session, pharmacy)
    _add_line(session, rid, priced_product)
    MACHINE.apply(session, rid, "SUBMIT_CART", BUYER)
    session.execute(text(
        "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
        "delivery_mode, goods_total) SELECT :oid, :onum, :r, vendor_id, :p, 'ACCEPTED', 'UPFRONT', "
        "'VENDOR_OWN_FLEET', 80 FROM vendor_offer LIMIT 1"
    ), {"oid": new_id("ord"), "onum": f"RQ-{rid}-V01", "r": rid, "p": pharmacy})
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, rid, "CANCEL", ADMIN)
    assert exc.value.code == "GUARD_FAILED" and exc.value.rule == "R-045"


def test_first_order_created_then_all_orders_terminal(session, pharmacy, priced_product):
    rid = _request(session, pharmacy)
    _add_line(session, rid, priced_product)
    MACHINE.apply(session, rid, "SUBMIT_CART", BUYER)
    row = MACHINE.apply(session, rid, "FIRST_ORDER_CREATED", "SYSTEM")
    assert row["status"] == "IN_FULFILMENT"
    row = MACHINE.apply(session, rid, "ALL_ORDERS_TERMINAL", "SYSTEM")
    assert row["status"] == "CLOSED"


def test_all_quotations_terminal_no_order_cancels_rfq_dead_end(session, pharmacy):
    rid = _request(session, pharmacy, status="CONFIRMED")
    org_id, ven_id = new_id("org"), new_id("ven")
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES (:o, :o, 'V1b', "
        "'v1b', 'VENDOR')"
    ), {"o": org_id})
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES (:v, :o, 'MAPUTO_CIDADE', 'V1b', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0)"
    ), {"v": ven_id, "o": org_id})
    qid = new_id("qtn")
    session.execute(text(
        "INSERT INTO quotation (id, request_id, vendor_id, status, invited_at, expires_at) VALUES "
        "(:id, :r, :v, 'EXPIRED', now(), now())"
    ), {"id": qid, "r": rid, "v": ven_id})

    row = MACHINE.apply(session, rid, "ALL_QUOTATIONS_TERMINAL_NO_ORDER", "SYSTEM")
    assert row["status"] == "CANCELLED"


def test_all_quotations_terminal_guard_fails_with_pending_quotation(session, pharmacy):
    rid = _request(session, pharmacy, status="CONFIRMED")
    org_id, ven_id = new_id("org"), new_id("ven")
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES (:o, :o, 'V1c', "
        "'v1c', 'VENDOR')"
    ), {"o": org_id})
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES (:v, :o, 'MAPUTO_CIDADE', 'V1c', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0)"
    ), {"v": ven_id, "o": org_id})
    session.execute(text(
        "INSERT INTO quotation (id, request_id, vendor_id, status, invited_at, expires_at) VALUES "
        "(:id, :r, :v, 'SUBMITTED', now(), now())"
    ), {"id": new_id("qtn"), "r": rid, "v": ven_id})
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, rid, "ALL_QUOTATIONS_TERMINAL_NO_ORDER", "SYSTEM")
    assert exc.value.code == "GUARD_FAILED"


def test_wrong_actor_forbidden(session, pharmacy):
    rid = _request(session, pharmacy)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, rid, "SUBMIT_CART_OVER_THRESHOLD", ADMIN)  # PharmacyBuyer-only
    assert exc.value.code == "FORBIDDEN"


def test_illegal_transition_matrix(session, pharmacy):
    rid = _request(session, pharmacy)
    assert_all_illegal(session, MACHINE, rid, BUYER)
