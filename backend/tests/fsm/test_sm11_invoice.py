"""A17 'Every state machine' row for SM-11 (invoice) — in particular the
defect-5 fix: `_effect_finalise`/`_effect_write_off` must skip the
`ledger_entry` write for UPFRONT/COD orders (a cash invoice must never move
`current_exposure` for a credit facility the order never drew on), and must
still write it for CREDIT_N_DAYS orders exactly as before."""
from decimal import Decimal

from sqlalchemy import text

from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

MACHINE = MACHINES["SM-11"]
SYSTEM = "SYSTEM"


def _make_order_and_invoice(session, suffix: str, payment_terms: str, amount: Decimal) -> tuple[str, str]:
    org_v, org_p = f"org_i_v_{suffix}", f"org_i_p_{suffix}"
    ven, pha, facility = f"ven_i_{suffix}", f"pha_i_{suffix}", f"crf_i_{suffix}"
    order, prod = f"ord_i_{suffix}", f"idx_i_{suffix}"
    req, line = f"req_i_{suffix}", f"rql_i_{suffix}"
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "(:v, :vt, 'V', 'v', 'VENDOR'), (:p, :pt, 'P', 'p', 'PHARMACY')"
    ), {"v": org_v, "vt": f"81{suffix}", "p": org_p, "pt": f"82{suffix}"})
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount) VALUES (:v, :o, 'MAPUTO_CIDADE', 'V', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0)"
    ), {"v": ven, "o": org_v})
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES (:p, :o, 'MAPUTO_CIDADE', 'A', 'P', 'X', 0, 0)"
    ), {"p": pha, "o": org_p})
    # a credit facility exists regardless — the point of defect 5 is that an
    # UPFRONT order must not touch it even though one is there to touch.
    session.execute(text(
        "INSERT INTO credit_facility (id, vendor_id, pharmacy_id, limit_amount, opening_balance, terms_days, "
        "status) VALUES (:f, :v, :p, 1000000.00, 0, 30, 'ACTIVE')"
    ), {"f": facility, "v": ven, "p": pha})
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES (:p, 'D', 'c', '1', '1', 'M', 'AUTHORISED', "
        "false, 'LOCAL:x', 'd')"
    ), {"p": prod})
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "(:r, :num, :p, 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ), {"r": req, "num": f"RQ-I-{suffix}", "p": pha})
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES (:l, :r, :p, 1)"
    ), {"l": line, "r": req, "p": prod})
    session.execute(text(
        "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
        "credit_days, delivery_mode, goods_total) VALUES (:o, :num, :r, :v, :p, 'RECEIPT_ACCEPTED', :terms, "
        "CASE WHEN :terms = 'CREDIT_N_DAYS' THEN 30 ELSE NULL END, 'VENDOR_OWN_FLEET', :amt)"
    ), {"o": order, "num": f"{suffix}-V01", "r": req, "v": ven, "p": pha, "terms": payment_terms, "amt": amount})
    invoice_id = f"inv_i_{suffix}"
    session.execute(text(
        "INSERT INTO invoice (id, order_id, issuer_vendor_id, pharmacy_id, vendor_invoice_number, total_amount, "
        "status, price_match_flag) VALUES (:i, :o, :v, :p, :num, :amt, 'UPLOADED', 'PENDING')"
    ), {"i": invoice_id, "o": order, "v": ven, "p": pha, "num": f"INV-{suffix}", "amt": amount})
    return order, invoice_id


def _ledger_count(session, invoice_id: str) -> int:
    return session.execute(
        text("SELECT count(*) FROM ledger_entry WHERE reference_type='invoice' AND reference_id=:i"),
        {"i": invoice_id},
    ).scalar()


def test_finalise_writes_ledger_entry_for_credit_n_days(session):
    order, invoice_id = _make_order_and_invoice(session, "credit", "CREDIT_N_DAYS", Decimal("170.00"))
    MACHINE.apply(session, invoice_id, "MATCH_OK", SYSTEM)
    row = MACHINE.apply(session, invoice_id, "FINALISE", SYSTEM)
    assert row["status"] == "AWAITING_PAYMENT"
    assert _ledger_count(session, invoice_id) == 1
    amount = session.execute(
        text("SELECT amount FROM ledger_entry WHERE reference_type='invoice' AND reference_id=:i"),
        {"i": invoice_id},
    ).scalar()
    assert amount == Decimal("170.00")


def test_finalise_skips_ledger_entry_for_upfront(session):
    """Defect 5: an UPFRONT invoice must leave the ledger untouched — the
    architect's reproduction was ledger going 0.00 -> 170.00 on a cash
    invoice for a pharmacy that chose UPFRONT *because* it had no headroom."""
    order, invoice_id = _make_order_and_invoice(session, "upfront", "UPFRONT", Decimal("170.00"))
    MACHINE.apply(session, invoice_id, "MATCH_OK", SYSTEM)
    row = MACHINE.apply(session, invoice_id, "FINALISE", SYSTEM)
    assert row["status"] == "AWAITING_PAYMENT"
    assert _ledger_count(session, invoice_id) == 0


def test_finalise_skips_ledger_entry_for_cod(session):
    order, invoice_id = _make_order_and_invoice(session, "cod", "COD", Decimal("50.00"))
    MACHINE.apply(session, invoice_id, "MATCH_OK", SYSTEM)
    MACHINE.apply(session, invoice_id, "FINALISE", SYSTEM)
    assert _ledger_count(session, invoice_id) == 0


def test_write_off_skips_contra_entry_for_upfront(session):
    order, invoice_id = _make_order_and_invoice(session, "wo", "UPFRONT", Decimal("50.00"))
    MACHINE.apply(session, invoice_id, "MATCH_OK", SYSTEM)
    MACHINE.apply(session, invoice_id, "FINALISE", SYSTEM)
    row = MACHINE.apply(session, invoice_id, "WRITE_OFF", Principal(user_id=None, roles=frozenset({"VendorFinance"})))
    assert row["status"] == "WRITTEN_OFF"
    assert _ledger_count(session, invoice_id) == 0
