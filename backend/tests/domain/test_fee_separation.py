"""A17 'Fees' rows / A11.1 — the platform never issues a goods invoice, and
goods figures are never touched by fees (executor manifest, "where the
architect will look hardest")."""
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

SQL_PATH = Path(__file__).resolve().parents[2] / "migrations" / "sql" / "0001_initial.sql"


def test_platform_invoice_never_references_invoice_table():
    sql = SQL_PATH.read_text()
    # platform_invoice's own CREATE TABLE block must not mention invoice/invoice_line
    start = sql.index("CREATE TABLE platform_invoice")
    end = sql.index(");", start)
    block = sql[start:end]
    assert "REFERENCES invoice" not in block
    assert "invoice_id" not in block
    assert "invoice_line_id" not in block


def test_fee_event_cannot_reference_invoice_line():
    sql = SQL_PATH.read_text()
    start = sql.index("CREATE TABLE fee_event")
    end = sql.index(");", start)
    block = sql[start:end]
    assert "references invoice" not in block.lower()  # would also catch "references invoice_line"
    assert re.search(r"\binvoice_id\b", block) is None  # platform_invoice_id is fine (group 7 -> group 7)
    assert re.search(r"\binvoice_line_id\b", block) is None


def test_goods_total_equals_sum_of_lines_and_is_never_a_fee(session):
    from rova.auth.principal import Principal
    from rova.domain.machines.registry import MACHINES
    from rova.ordering.checkout import checkout as run_checkout

    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "('org_fs_v', '941', 'V', 'v', 'VENDOR'), ('org_fs_p', '942', 'P', 'p', 'PHARMACY')"
    ))
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount, acceptance_mode, status) VALUES ('ven_fs', 'org_fs_v', 'MAPUTO_CIDADE', 'V', "
        "'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0, 'AUTO_ACCEPT_FULL', 'ACTIVE')"
    ))
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES ('pha_fs', 'org_fs_p', 'MAPUTO_CIDADE', 'A', 'P', 'X', 0, 0)"
    ))
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        "('idx_fs', 'D', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x', 'd')"
    ))
    session.execute(text(
        "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, pack_size, "
        "expiry_horizon_days, price, stock_confirmed_at) VALUES "
        "('ofr_fs', 'ven_fs', 'idx_fs', false, 100, '1', 300, 20.00, now())"
    ))
    # GLOBAL pharmacy service fee schedule + a pharmacy WITHOUT consent (R-147 demo)
    session.execute(text(
        "INSERT INTO app_user (id, phone, name, password_hash) VALUES ('usr_fs', '+258000666001', 'U', 'x')"
    ))
    session.execute(text(
        "INSERT INTO fee_schedule (id, type, payer, rate_or_amount, applies_to, scope_type, scope_id, "
        "effective_from, earning_event, legal_status, created_by_user_id) VALUES "
        "('fsc_fs', 'PHARMACY_SERVICE_FEE', 'PHARMACY', 150.00, 'ALL_ORDERS', 'GLOBAL', NULL, '2026-01-01', "
        "'RECEIPT_ACCEPTED', 'STANDARD', 'usr_fs')"
    ))
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "('req_fs', 'RQ-FS-1', 'pha_fs', 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ))
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES "
        "('rql_fs', 'req_fs', 'idx_fs', 4)"
    ))
    session.flush()

    buyer = Principal(user_id="usr_fs", membership_id="mem_fs", organisation_id="org_fs_p",
                       roles=frozenset({"PharmacyBuyer"}), surface="PH", pharmacy_id="pha_fs",
                       vendor_id=None, transporter_id=None)
    result = run_checkout(session, request_id="req_fs", actor=buyer)
    order_id = result["orders"][0]["id"]

    order = session.execute(text('SELECT goods_total FROM "order" WHERE id=:o'), {"o": order_id}).mappings().one()
    lines_sum = session.execute(
        text("SELECT SUM(unit_price * ordered_qty) FROM order_line WHERE order_id=:o"), {"o": order_id}
    ).scalar()
    assert order["goods_total"] == lines_sum == Decimal("80.00")

    # no consent -> no PHARMACY_SERVICE_FEE captured (R-147); goods_total untouched either way
    assert result["orders"][0]["platform_fees"] == []
    captured = session.execute(
        text("SELECT count(*) FROM order_fee_schedule WHERE order_id=:o AND fee_schedule_id='fsc_fs'"),
        {"o": order_id},
    ).scalar()
    assert captured == 0

    # no fee_event exists yet — accrual only fires at RECEIPT_ACCEPTED
    assert session.execute(text("SELECT count(*) FROM fee_event WHERE order_id=:o"), {"o": order_id}).scalar() == 0

    session.execute(
        text("UPDATE order_line SET confirmed_qty = ordered_qty, fulfilment_status='LINE_FULL' WHERE order_id=:o"),
        {"o": order_id},
    )
    MACHINES["SM-03"].apply(session, order_id, "ACCEPT", buyer.__class__(
        user_id="usr_fs", membership_id="mem_fs", organisation_id="org_fs_v", roles=frozenset({"VendorOrderDesk"}),
        surface="VN", pharmacy_id=None, vendor_id="ven_fs", transporter_id=None))
    session.execute(text(
        "UPDATE order_line SET batch_number='B', lot_number='L', expiry_date=:exp, seal_ids=ARRAY['S1'] "
        "WHERE order_id=:o"
    ), {"exp": date(2027, 1, 1), "o": order_id})
    MACHINES["SM-03"].apply(session, order_id, "DISPATCH", buyer.__class__(
        user_id="usr_fs", membership_id="mem_fs", organisation_id="org_fs_v", roles=frozenset({"VendorOrderDesk"}),
        surface="VN", pharmacy_id=None, vendor_id="ven_fs", transporter_id=None))
    MACHINES["SM-03"].apply(session, order_id, "DELIVERED", "SYSTEM")
    receiver = buyer.__class__(user_id="usr_fs", membership_id="mem_fs", organisation_id="org_fs_p",
                                roles=frozenset({"PharmacyReceiver"}), surface="PH", pharmacy_id="pha_fs",
                                vendor_id=None, transporter_id=None)
    MACHINES["SM-03"].apply(session, order_id, "RECEIPT_ACCEPT", receiver)

    order_after = session.execute(text('SELECT goods_total FROM "order" WHERE id=:o'), {"o": order_id}).mappings().one()
    assert order_after["goods_total"] == Decimal("80.00")  # unchanged by any fee (A11.1)
    session.rollback()
