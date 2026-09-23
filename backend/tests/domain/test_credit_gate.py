"""A17 'Credit gate' row — a pharmacy near its limit cannot stack orders past
it (executor manifest, "where the architect will look hardest")."""
from decimal import Decimal

from sqlalchemy import text

from rova.credit.exposure import PENDING_EXPOSURE_STATES, current_exposure, pending_exposure
from rova.credit.gate import check


def _make_pair(session, suffix: str, limit: Decimal, opening: Decimal = Decimal("0")):
    org_v, org_p = f"org_cg_v_{suffix}", f"org_cg_p_{suffix}"
    ven, pha, facility = f"ven_cg_{suffix}", f"pha_cg_{suffix}", f"crf_cg_{suffix}"
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:o, :tax, 'V', 'v', 'VENDOR')"
    ), {"o": org_v, "tax": f"91{suffix}"})
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES (:v, :o, 'MAPUTO_CIDADE', 'V', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0)"
    ), {"v": ven, "o": org_v})
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:o, :tax, 'P', 'p', 'PHARMACY')"
    ), {"o": org_p, "tax": f"92{suffix}"})
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES (:p, :o, 'MAPUTO_CIDADE', 'A', 'P', 'X', 0, 0)"
    ), {"p": pha, "o": org_p})
    session.execute(text(
        "INSERT INTO credit_facility (id, vendor_id, pharmacy_id, limit_amount, opening_balance, "
        "opening_balance_attested_at, terms_days) VALUES (:f, :v, :p, :lim, :ob, "
        "CASE WHEN :ob > 0 THEN now() ELSE NULL END, 30)"
    ), {"f": facility, "v": ven, "p": pha, "lim": limit, "ob": opening})
    return ven, pha, facility


def _make_order(session, suffix: str, ven: str, pha: str, status: str, ordered_qty: int, confirmed_qty: int,
                 unit_price: Decimal, with_ok_invoice: bool = False):
    req, order, line = f"req_cg_{suffix}", f"ord_cg_{suffix}", f"orl_cg_{suffix}"
    prod = f"idx_cg_{suffix}"
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES (:p, 'D', 'c', '1', '1', 'M', 'AUTHORISED', "
        "false, 'LOCAL:x', 'd')"
    ), {"p": prod})
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "(:r, :num, :p, 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ), {"r": req, "num": f"RQ-CG-{suffix}", "p": pha})
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES (:l, :r, :p, :q)"
    ), {"l": f"rql_cg_{suffix}", "r": req, "p": prod, "q": ordered_qty})
    session.execute(text(
        "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
        "credit_days, delivery_mode, goods_total) VALUES (:o, :num, :r, :v, :p, :s, 'CREDIT_N_DAYS', 30, "
        "'VENDOR_OWN_FLEET', 0)"
    ), {"o": order, "num": f"{suffix}-V01", "r": req, "v": ven, "p": pha, "s": status})
    session.execute(text(
        "INSERT INTO order_line (id, order_id, request_line_id, index_product_id, regulated_price, ordered_qty, "
        "confirmed_qty, unit_price, price_source, price_source_id) VALUES "
        "(:l, :o, :rl, :p, false, :oq, :cq, :price, 'VENDOR_OFFER', 'ofr_x')"
    ), {"l": line, "o": order, "rl": f"rql_cg_{suffix}", "p": prod, "oq": ordered_qty, "cq": confirmed_qty,
        "price": unit_price})
    if with_ok_invoice:
        session.execute(text(
            "INSERT INTO invoice (id, order_id, issuer_vendor_id, pharmacy_id, vendor_invoice_number, "
            "total_amount, status, price_match_flag) VALUES (:i, :o, :v, :p, :num, :amt, 'PRICE_MATCH_OK', 'OK')"
        ), {"i": f"inv_cg_{suffix}", "o": order, "v": ven, "p": pha, "num": f"INV-{suffix}",
            "amt": unit_price * confirmed_qty})
    return order


def test_pending_exposure_covers_all_six_states_and_excludes_ok_invoice(session):
    ven, pha, _ = _make_pair(session, "states", Decimal("1000000"))
    assert set(PENDING_EXPOSURE_STATES) == {
        "PENDING_ACCEPTANCE", "ACCEPTED", "DISPATCHED", "DELIVERED_PENDING_RECEIPT", "RECEIPT_ACCEPTED", "DISPUTED",
    }
    for i, status in enumerate(PENDING_EXPOSURE_STATES):
        _make_order(session, f"states{i}", ven, pha, status, ordered_qty=10, confirmed_qty=8, unit_price=Decimal("10.00"))
    # one order per state: PENDING_ACCEPTANCE counts ordered_qty (10*10=100), the
    # other five count confirmed_qty (8*10=80 each) => 100 + 5*80 = 500
    total = pending_exposure(session, ven, pha)
    assert total == Decimal("500.00")

    # a terminal state (CLOSED) must NOT count
    _make_order(session, "closed", ven, pha, "CLOSED", ordered_qty=10, confirmed_qty=10, unit_price=Decimal("10.00"))
    assert pending_exposure(session, ven, pha) == Decimal("500.00")

    # an order with a price-match-OK invoice is excluded even in a counted state
    _make_order(session, "invoiced", ven, pha, "RECEIPT_ACCEPTED", ordered_qty=10, confirmed_qty=10,
                unit_price=Decimal("10.00"), with_ok_invoice=True)
    assert pending_exposure(session, ven, pha) == Decimal("500.00")


def test_current_exposure_includes_opening_balance_and_ledger_contra_entries(session):
    ven, pha, facility = _make_pair(session, "ledger", Decimal("1000000"), opening=Decimal("200.00"))
    assert current_exposure(session, facility) == Decimal("200.00")
    session.execute(text(
        "INSERT INTO ledger_entry (id, credit_facility_id, entry_type, reference_type, reference_id, amount) "
        "VALUES ('led_cg_1', :f, 'INVOICE', 'invoice', 'inv_x', 300.00)"
    ), {"f": facility})
    assert current_exposure(session, facility) == Decimal("500.00")
    # a contra entry (ADJUSTMENT) is included too
    session.execute(text(
        "INSERT INTO ledger_entry (id, credit_facility_id, entry_type, reference_type, reference_id, amount) "
        "VALUES ('led_cg_2', :f, 'ADJUSTMENT', 'invoice_write_off', 'inv_x', -100.00)"
    ), {"f": facility})
    assert current_exposure(session, facility) == Decimal("400.00")


def test_gate_blocks_when_current_plus_pending_plus_new_exceeds_limit(session):
    ven, pha, facility = _make_pair(session, "block", Decimal("1000.00"))
    _make_order(session, "block1", ven, pha, "ACCEPTED", ordered_qty=10, confirmed_qty=10, unit_price=Decimal("80.00"))
    # pending = 800; a new 300 order would push to 1100 > 1000 -> blocked
    result = check(session, facility, ven, pha, Decimal("300.00"))
    assert result.blocked is True
    assert set(result.options) == {"UPFRONT", "SPLIT", "REROUTE", "ADMIN_OVERRIDE"}
    # a smaller order (150) fits within headroom (200) -> not blocked
    result2 = check(session, facility, ven, pha, Decimal("150.00"))
    assert result2.blocked is False
    assert result2.options == []


def test_gate_blocks_when_facility_suspended_even_with_headroom(session):
    ven, pha, facility = _make_pair(session, "susp", Decimal("1000000.00"))
    session.execute(text("UPDATE credit_facility SET status='SUSPENDED' WHERE id=:f"), {"f": facility})
    result = check(session, facility, ven, pha, Decimal("1.00"))
    assert result.blocked is True  # R-044
