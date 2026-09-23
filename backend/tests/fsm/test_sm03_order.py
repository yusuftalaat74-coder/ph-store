"""A17 'Every state machine' row for SM-03 (the implemented subset — see
manifest for the triggers of the full A4.2 table not wired this session)."""
from datetime import date

import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

MACHINE = MACHINES["SM-03"]

VENDOR_DESK = Principal(user_id=None, roles=frozenset({"VendorOrderDesk"}))
PHARMACY_ADMIN = Principal(user_id=None, roles=frozenset({"PharmacyAdmin"}))
COURIER = Principal(user_id=None, roles=frozenset({"Courier"}))


@pytest.fixture
def order(session):
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "('org_sm3_v', '981', 'V', 'v', 'VENDOR'), ('org_sm3_p', '982', 'P', 'p', 'PHARMACY')"
    ))
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES ('ven_sm3', 'org_sm3_v', 'MAPUTO_CIDADE', 'V', 'IMPORTER_WHOLESALER', "
        "'VENDOR_OWN_FLEET', 0)"
    ))
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES ('pha_sm3', 'org_sm3_p', 'MAPUTO_CIDADE', 'A', 'P', 'X', 0, 0)"
    ))
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        "('idx_sm3', 'D', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x', 'd')"
    ))
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "('req_sm3', 'RQ-SM3-1', 'pha_sm3', 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ))
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES "
        "('rql_sm3', 'req_sm3', 'idx_sm3', 5)"
    ))
    session.execute(text(
        "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
        "delivery_mode, goods_total) VALUES ('ord_sm3', 'RQ-SM3-1-V01', 'req_sm3', 'ven_sm3', 'pha_sm3', "
        "'PENDING_ACCEPTANCE', 'UPFRONT', 'VENDOR_OWN_FLEET', 100)"
    ))
    session.execute(text(
        "INSERT INTO order_line (id, order_id, request_line_id, index_product_id, regulated_price, ordered_qty, "
        "confirmed_qty, unit_price, price_source, price_source_id) VALUES "
        "('orl_sm3', 'ord_sm3', 'rql_sm3', 'idx_sm3', false, 5, 5, 20.00, 'VENDOR_OFFER', 'ofr_x')"
    ))
    yield "ord_sm3"
    session.rollback()


def test_legal_accept_transition(session, order):
    row = MACHINE.apply(session, order, "ACCEPT", VENDOR_DESK)
    assert row["status"] == "ACCEPTED"
    assert row["accepted_at"] is not None
    st = session.execute(
        text("SELECT * FROM state_transition WHERE subject_id=:o ORDER BY occurred_at DESC LIMIT 1"), {"o": order}
    ).mappings().one()
    assert st["from_state"] == "PENDING_ACCEPTANCE" and st["to_state"] == "ACCEPTED" and st["trigger"] == "ACCEPT"


def test_illegal_transition_raises(session, order):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, order, "DISPATCH", VENDOR_DESK)  # can't dispatch before acceptance
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_wrong_actor_role_forbidden(session, order):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, order, "ACCEPT", PHARMACY_ADMIN)
    assert exc.value.code == "FORBIDDEN"


def test_dispatch_guard_r054_fails_without_seals(session, order):
    MACHINE.apply(session, order, "ACCEPT", VENDOR_DESK)
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, order, "DISPATCH", VENDOR_DESK)
    assert exc.value.code == "GUARD_FAILED"
    assert exc.value.rule == "R-054"


def test_dispatch_guard_r121_fails_with_undecided_short_line(session, order):
    """Defect 3 fix: _guard_dispatch used to check R-054 only, so an order
    with an undecided LINE_SHORT (its REROUTE_ASK timer still RUNNING)
    dispatched with a 200 — and the timer later DROPped a line out from
    under an order that had already shipped."""
    MACHINE.apply(session, order, "ACCEPT", VENDOR_DESK)
    session.execute(text(
        "UPDATE order_line SET fulfilment_status='LINE_SHORT', confirmed_qty=2, "
        "batch_number='B', lot_number='L', expiry_date=:exp, seal_ids=ARRAY['S1'] WHERE id='orl_sm3'"
    ), {"exp": date(2027, 1, 1)})
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, order, "DISPATCH", VENDOR_DESK)
    assert exc.value.code == "GUARD_FAILED"
    assert exc.value.rule == "R-121"


def test_dispatch_succeeds_with_seals_then_full_lifecycle(session, order):
    MACHINE.apply(session, order, "ACCEPT", VENDOR_DESK)
    session.execute(text(
        "UPDATE order_line SET batch_number='B', lot_number='L', expiry_date=:exp, seal_ids=ARRAY['S1'] "
        "WHERE id='orl_sm3'"
    ), {"exp": date(2027, 1, 1)})
    row = MACHINE.apply(session, order, "DISPATCH", VENDOR_DESK)
    assert row["status"] == "DISPATCHED" and row["dispatched_at"] is not None
    row = MACHINE.apply(session, order, "DELIVERED", "SYSTEM")
    assert row["status"] == "DELIVERED_PENDING_RECEIPT"
    row = MACHINE.apply(session, order, "RECEIPT_ACCEPT", PHARMACY_ADMIN)
    assert row["status"] == "RECEIPT_ACCEPTED"

# test_stale_state_conflict used to live here and was a sham: it raised
# ApiError("STALE_STATE", ...) from inside the test body itself (forging the
# race it claimed to prove protection against), so it could never fail no
# matter what fsm.py's actual STALE_STATE branch did. Deleted per the
# six-defect repair round's "also fix" list — the real STALE_STATE coverage
# is tests/fsm/test_sm10_return.py's genuine two-`threading.Thread` test
# against `MACHINE.apply` itself.
