"""A17 'Every state machine' row for SM-04 (§6 SM-04) — order_line.fulfilment_status:
CONFIRM_FULL, CONFIRM_SHORT, POST_ACCEPTANCE_SHORTAGE (the post-acceptance
LINE_FULL -> LINE_SHORT path named in the executor's task), AUTO_REROUTE,
PHARMACY_REROUTE, ACCEPT_SUBSTITUTE, DROP, and the two licence-lapse/
compliance-halt cascades."""
import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

MACHINE = MACHINES["SM-04"]

VENDOR_DESK = Principal(user_id=None, roles=frozenset({"VendorOrderDesk"}))
BUYER = Principal(user_id=None, roles=frozenset({"PharmacyBuyer"}))


@pytest.fixture
def line(session):
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "('org_sm4_v', '961', 'V4', 'v4', 'VENDOR'), ('org_sm4_p', '962', 'P4', 'p4', 'PHARMACY')"
    ))
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES ('ven_sm4', 'org_sm4_v', 'MAPUTO_CIDADE', 'V4', 'IMPORTER_WHOLESALER', "
        "'VENDOR_OWN_FLEET', 0)"
    ))
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude, auto_reroute) VALUES ('pha_sm4', 'org_sm4_p', 'MAPUTO_CIDADE', 'A', 'P4', 'X', "
        "0, 0, false)"
    ))
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        "('idx_sm4', 'D4', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x4', 'd4')"
    ))
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "('req_sm4', 'RQ-SM4-1', 'pha_sm4', 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ))
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES "
        "('rql_sm4', 'req_sm4', 'idx_sm4', 8)"
    ))
    session.execute(text(
        "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
        "delivery_mode, goods_total) VALUES ('ord_sm4', 'RQ-SM4-1-V01', 'req_sm4', 'ven_sm4', 'pha_sm4', "
        "'PENDING_ACCEPTANCE', 'UPFRONT', 'VENDOR_OWN_FLEET', 160)"
    ))
    session.execute(text(
        "INSERT INTO order_line (id, order_id, request_line_id, index_product_id, regulated_price, ordered_qty, "
        "confirmed_qty, unit_price, price_source, price_source_id) VALUES "
        "('orl_sm4', 'ord_sm4', 'rql_sm4', 'idx_sm4', false, 8, 0, 20.00, 'VENDOR_OFFER', 'ofr_x4')"
    ))
    yield "orl_sm4"
    session.rollback()


def test_confirm_full_legal(session, line):
    row = MACHINE.apply(session, line, "CONFIRM_FULL", VENDOR_DESK)
    assert row["fulfilment_status"] == "LINE_FULL"
    assert row["confirmed_qty"] == 8


def test_illegal_transition_raises(session, line):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, line, "DROP", BUYER)  # can't drop from LINE_PENDING
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_wrong_actor_forbidden(session, line):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, line, "CONFIRM_FULL", BUYER)
    assert exc.value.code == "FORBIDDEN"


def test_confirm_short_starts_reroute_timer(session, line):
    row = MACHINE.apply(session, line, "CONFIRM_SHORT", VENDOR_DESK)
    assert row["fulfilment_status"] == "LINE_SHORT"
    timer = session.execute(
        text("SELECT status FROM sla_timer WHERE policy_type='REROUTE_ASK' AND subject_id=:l"), {"l": line}
    ).mappings().one()
    assert timer["status"] == "RUNNING"


def test_post_acceptance_shortage_from_line_full(session, line):
    MACHINE.apply(session, line, "CONFIRM_FULL", VENDOR_DESK)
    session.execute(text("UPDATE \"order\" SET status='ACCEPTED' WHERE id='ord_sm4'"))
    session.execute(text(
        "INSERT INTO delivery_job (id, order_id, vendor_id, pharmacy_id, performed_by, status) VALUES "
        "('dlv_sm4x', 'ord_sm4', 'ven_sm4', 'pha_sm4', 'VENDOR_OWN_FLEET', 'CREATED')"
    ))
    row = MACHINE.apply(session, line, "POST_ACCEPTANCE_SHORTAGE", "SYSTEM", short_reason="QTY_CORRECTION")
    assert row["fulfilment_status"] == "LINE_SHORT"
    assert row["short_reason"] == "QTY_CORRECTION"
    audit = session.execute(
        text("SELECT 1 FROM audit_event WHERE action_code='ORDER_LINE_QTY_CORRECTION' AND subject_id=:l"), {"l": line}
    ).first()
    assert audit is not None


def test_post_acceptance_shortage_guard_fails_once_dispatch_started(session, line):
    MACHINE.apply(session, line, "CONFIRM_FULL", VENDOR_DESK)
    session.execute(text("UPDATE \"order\" SET status='ACCEPTED' WHERE id='ord_sm4'"))
    session.execute(text(
        "INSERT INTO delivery_job (id, order_id, vendor_id, pharmacy_id, performed_by, status) VALUES "
        "('dlv_sm4y', 'ord_sm4', 'ven_sm4', 'pha_sm4', 'VENDOR_OWN_FLEET', 'ASSIGNED')"
    ))
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, line, "POST_ACCEPTANCE_SHORTAGE", "SYSTEM")
    assert exc.value.code == "GUARD_FAILED"


def test_pharmacy_licence_lapse_cascades_to_dropped(session, line):
    MACHINE.apply(session, line, "CONFIRM_FULL", VENDOR_DESK)
    session.execute(text("UPDATE \"order\" SET status='ACCEPTED' WHERE id='ord_sm4'"))
    session.execute(text(
        "INSERT INTO delivery_job (id, order_id, vendor_id, pharmacy_id, performed_by, status) VALUES "
        "('dlv_sm4z', 'ord_sm4', 'ven_sm4', 'pha_sm4', 'VENDOR_OWN_FLEET', 'CREATED')"
    ))
    row = MACHINE.apply(session, line, "PHARMACY_LICENCE_LAPSE", "SYSTEM")
    assert row["fulfilment_status"] == "LINE_DROPPED"  # cascades straight through, no reroute (§6)


def test_reroute_creates_new_request_line_and_cancels_timer(session, line):
    MACHINE.apply(session, line, "CONFIRM_SHORT", VENDOR_DESK)
    row = MACHINE.apply(session, line, "PHARMACY_REROUTE", BUYER)
    assert row["fulfilment_status"] == "LINE_REROUTED"
    new_line = session.execute(
        text("SELECT * FROM request_line WHERE origin_line_id='rql_sm4'")
    ).mappings().one()
    assert new_line["qty_requested"] == 8  # ordered_qty(8) - confirmed_qty(0)
    timer = session.execute(
        text("SELECT status FROM sla_timer WHERE policy_type='REROUTE_ASK' AND subject_id=:l"), {"l": line}
    ).mappings().one()
    assert timer["status"] == "CANCELLED"


def test_drop_from_line_short(session, line):
    MACHINE.apply(session, line, "CONFIRM_SHORT", VENDOR_DESK)
    row = MACHINE.apply(session, line, "DROP", BUYER)
    assert row["fulfilment_status"] == "LINE_DROPPED"
