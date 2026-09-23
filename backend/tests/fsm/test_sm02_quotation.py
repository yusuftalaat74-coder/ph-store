"""A17 'Every state machine' row for SM-02 (§6 SM-02) — the RFQ quotation
lifecycle: SUBMIT, QUOTE_TIMEOUT, ACCEPT_LINES (R-122 buyer/admin split),
DECLINE, EXPIRE_UNANSWERED. `ACCEPT_LINES` also exercises the SM-03 Order +
order_line + allocation rows it creates directly (rova/domain/machines/sm02_quotation.py)."""
from datetime import timedelta

import pytest
from sqlalchemy import text

from rova.core.clock import now
from rova.core.errors import ApiError
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

MACHINE = MACHINES["SM-02"]

VENDOR_DESK = Principal(user_id=None, roles=frozenset({"VendorOrderDesk"}))
BUYER = Principal(user_id=None, roles=frozenset({"PharmacyBuyer"}))
PHARMACY_ADMIN = Principal(user_id=None, roles=frozenset({"PharmacyAdmin"}))


@pytest.fixture
def quotation(session):
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "('org_sm2_v', '971', 'V2', 'v2', 'VENDOR'), ('org_sm2_p', '972', 'P2', 'p2', 'PHARMACY')"
    ))
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES ('ven_sm2', 'org_sm2_v', 'MAPUTO_CIDADE', 'V2', 'IMPORTER_WHOLESALER', "
        "'VENDOR_OWN_FLEET', 0)"
    ))
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES ('pha_sm2', 'org_sm2_p', 'MAPUTO_CIDADE', 'A', 'P2', 'X', 0, 0)"
    ))
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        "('idx_sm2', 'D2', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x2', 'd2')"
    ))
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "('req_sm2', 'RQ-SM2-1', 'pha_sm2', 'RFQ', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ))
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES "
        "('rql_sm2', 'req_sm2', 'idx_sm2', 10)"
    ))
    expires_at = now() + timedelta(hours=24)
    session.execute(text(
        "INSERT INTO quotation (id, request_id, vendor_id, status, expires_at) VALUES "
        "('quo_sm2', 'req_sm2', 'ven_sm2', 'INVITED', :exp)"
    ), {"exp": expires_at})
    # SlaTimer(QUOTATION): started by the (not-yet-built) quotation-invite
    # service when the quotation row is created — inserted directly here,
    # matching quotation.expires_at, so _guard_submit's `timers.is_running`
    # check (A5) has something to find, the same way a real invite would.
    session.execute(text(
        "INSERT INTO sla_timer (id, policy_type, subject_type, subject_id, expires_at, config_source) VALUES "
        "('sla_sm2_quo', 'QUOTATION', 'quotation', 'quo_sm2', :exp, 'CFG-SLA-QUOTATION-HOURS')"
    ), {"exp": expires_at})
    session.execute(text(
        "INSERT INTO quotation_line (id, quotation_id, request_line_id, index_product_id, regulated_price, "
        "offered_qty, price, expiry_horizon_days) VALUES "
        "('qul_sm2', 'quo_sm2', 'rql_sm2', 'idx_sm2', false, 10, 15.00, 400)"
    ))
    yield "quo_sm2"
    session.rollback()


def test_legal_submit_transition(session, quotation):
    row = MACHINE.apply(session, quotation, "SUBMIT", VENDOR_DESK)
    assert row["status"] == "SUBMITTED"
    assert row["submitted_at"] is not None


def test_illegal_transition_raises(session, quotation):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, quotation, "ACCEPT_LINES", BUYER)  # can't accept before SUBMIT
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_wrong_actor_role_forbidden(session, quotation):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, quotation, "SUBMIT", BUYER)
    assert exc.value.code == "FORBIDDEN"


def test_accept_lines_creates_order(session, quotation):
    MACHINE.apply(session, quotation, "SUBMIT", VENDOR_DESK)
    row = MACHINE.apply(session, quotation, "ACCEPT_LINES", BUYER,
                         accept_lines=[{"quotation_line_id": "qul_sm2", "accepted_qty": 10}])
    assert row["status"] == "ACCEPTED"
    order = session.execute(
        text('SELECT * FROM "order" WHERE request_id=:r'), {"r": "req_sm2"}
    ).mappings().one()
    assert order["vendor_id"] == "ven_sm2"
    assert order["status"] == "PENDING_ACCEPTANCE"
    line = session.execute(
        text("SELECT * FROM order_line WHERE order_id=:o"), {"o": order["id"]}
    ).mappings().one()
    assert line["ordered_qty"] == 10
    # SM-02's ACCEPT_LINES effect also starts SlaTimer(ACCEPTANCE) and creates
    # the order's one open eta_estimate (SM-20), mirroring checkout.py's path.
    timer = session.execute(
        text("SELECT status FROM sla_timer WHERE policy_type='ACCEPTANCE' AND subject_id=:o"), {"o": order["id"]}
    ).mappings().one()
    assert timer["status"] == "RUNNING"
    eta = session.execute(text("SELECT status FROM eta_estimate WHERE order_id=:o"), {"o": order["id"]}).mappings().one()
    assert eta["status"] == "PROVISIONAL"


def test_decline_transition(session, quotation):
    MACHINE.apply(session, quotation, "SUBMIT", VENDOR_DESK)
    row = MACHINE.apply(session, quotation, "DECLINE", BUYER)
    assert row["status"] == "DECLINED"


def test_quote_timeout_is_system_only(session, quotation):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, quotation, "QUOTE_TIMEOUT", VENDOR_DESK)
    assert exc.value.code == "FORBIDDEN"
    row = MACHINE.apply(session, quotation, "QUOTE_TIMEOUT", "SYSTEM")
    assert row["status"] == "EXPIRED"


def test_accept_lines_over_threshold_requires_pharmacy_admin(session, quotation):
    session.execute(text("UPDATE pharmacy_account SET buyer_approval_threshold=50 WHERE id='pha_sm2'"))
    MACHINE.apply(session, quotation, "SUBMIT", VENDOR_DESK)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, quotation, "ACCEPT_LINES", BUYER,
                       accept_lines=[{"quotation_line_id": "qul_sm2", "accepted_qty": 10}])  # 10*15=150 > 50
    assert exc.value.code == "GUARD_FAILED"
    assert exc.value.rule == "R-122"
    row = MACHINE.apply(session, quotation, "ACCEPT_LINES", PHARMACY_ADMIN,
                         accept_lines=[{"quotation_line_id": "qul_sm2", "accepted_qty": 10}])
    assert row["status"] == "ACCEPTED"
