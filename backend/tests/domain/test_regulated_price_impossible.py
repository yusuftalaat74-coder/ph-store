"""A17 'Regulated price' row — the regulated-price rule must be structurally
impossible to violate, not merely validated (executor manifest, backend-spec
"where the architect will look hardest"). Cases (1)-(4) and (7) are proven at
the database level directly against a real transaction; the DB, not the
application, must reject the illegal insert."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from rova.core.clock import now


@pytest.fixture
def regulated_product(session):
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) "
        "VALUES ('org_rp_test', '900000001', 'RP Vendor', 'rp vendor', 'VENDOR')"
    ))
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
        "delivery_mode, mov_amount) VALUES ('ven_rp_test', 'org_rp_test', 'MAPUTO_CIDADE', 'RP Vendor', "
        "'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0)"
    ))
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        "('idx_rp_test', 'TestDrug', 'comprimido', '1mg', '1', 'M', 'AUTHORISED', true, 'LOCAL:x', 'testdrug')"
    ))
    yield "idx_rp_test"
    session.rollback()


def test_1_vendor_offer_with_price_on_regulated_rejected_by_db(session, regulated_product):
    with pytest.raises(IntegrityError, match="ck_vendor_offer_regulated_no_price"):
        session.execute(text(
            "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
            "pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES "
            "('ofr_rp_illegal', 'ven_rp_test', :p, true, 10, '1', 365, 99.00, :now)"
        ), {"p": regulated_product, "now": now()})
        session.flush()
    session.rollback()


def test_2_quotation_line_with_price_on_regulated_rejected_by_db(session, regulated_product):
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) SELECT 'pha_rp_test', id, 'MAPUTO_CIDADE', 'A', 'X', 'X', 0, 0 "
        "FROM organisation WHERE id='org_rp_test'"
    ))
    # (uses a throwaway pharmacy org so the FK on request/quotation resolves)
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) "
        "VALUES ('org_rp_pha', '900000002', 'RP Pharmacy', 'rp pharmacy', 'PHARMACY')"
    ))
    session.execute(text(
        "UPDATE pharmacy_account SET organisation_id='org_rp_pha' WHERE id='pha_rp_test'"
    ))
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "('req_rp_test', 'RQ-RP-TEST', 'pha_rp_test', 'RFQ', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ))
    session.execute(text(
        "INSERT INTO quotation (id, request_id, vendor_id, expires_at) VALUES "
        "('quo_rp_test', 'req_rp_test', 'ven_rp_test', :exp)"
    ), {"exp": now() + timedelta(hours=1)})
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested, line_kind) VALUES "
        "('rql_rp_test', 'req_rp_test', :p, 1, 'RFQ')"
    ), {"p": regulated_product})

    with pytest.raises(IntegrityError, match="ck_quotation_line_regulated_no_price"):
        session.execute(text(
            "INSERT INTO quotation_line (id, quotation_id, request_line_id, index_product_id, regulated_price, "
            "offered_qty, price, expiry_horizon_days) VALUES "
            "('qul_rp_illegal', 'quo_rp_test', 'rql_rp_test', :p, true, 1, 50.00, 100)"
        ), {"p": regulated_product})
        session.flush()
    session.rollback()


def test_3_order_line_vendor_offer_source_on_regulated_rejected_by_db(session, regulated_product):
    # order_line's regulated line must come from PRICE_REFERENCE, never
    # VENDOR_OFFER (R-008/R-011) — enforced by ck_order_line_price_source.
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) "
        "VALUES ('org_rp_pha2', '900000003', 'RP Pharmacy 2', 'rp pharmacy 2', 'PHARMACY')"
    ))
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES ('pha_rp_test2', 'org_rp_pha2', 'MAPUTO_CIDADE', 'A', 'X', 'X', 0, 0)"
    ))
    session.execute(text(
        "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
        "('req_rp_test2', 'RQ-RP-TEST2', 'pha_rp_test2', 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
    ))
    session.execute(text(
        "INSERT INTO request_line (id, request_id, index_product_id, qty_requested) VALUES "
        "('rql_rp_test2', 'req_rp_test2', :p, 1)"
    ), {"p": regulated_product})
    session.execute(text(
        "INSERT INTO \"order\" (id, number, request_id, vendor_id, pharmacy_id, status, payment_terms, "
        "delivery_mode, goods_total) VALUES ('ord_rp_test', 'RQ-RP-TEST2-V01', 'req_rp_test2', "
        "'ven_rp_test', 'pha_rp_test2', 'PENDING_ACCEPTANCE', 'UPFRONT', 'VENDOR_OWN_FLEET', 0)"
    ))

    with pytest.raises(IntegrityError, match="ck_order_line_price_source"):
        session.execute(text(
            "INSERT INTO order_line (id, order_id, request_line_id, index_product_id, regulated_price, "
            "ordered_qty, unit_price, price_source, price_source_id) VALUES "
            "('orl_rp_illegal', 'ord_rp_test', 'rql_rp_test2', :p, true, 1, 50.00, 'VENDOR_OFFER', 'ofr_x')"
        ), {"p": regulated_product})
    session.rollback()


def test_4_changing_regulated_price_once_referenced_is_blocked_by_fk(session, regulated_product):
    session.execute(text(
        "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
        "pack_size, expiry_horizon_days, stock_confirmed_at) VALUES "
        "('ofr_rp_legal', 'ven_rp_test', :p, true, 10, '1', 365, :now)"
    ), {"p": regulated_product, "now": now()})
    with pytest.raises(IntegrityError):
        session.execute(text("UPDATE index_product SET regulated_price = false WHERE id=:p"),
                         {"p": regulated_product})
    session.rollback()


def test_5_vendor_principal_token_and_get_principal_roundtrip(client, session, regulated_product):
    """Was `test_5_api_offer_create_with_price_on_regulated_returns_422`,
    asserting only `assert token` (item 5, backend-review-r1.md) — its own
    docstring already said no `POST /v1/vendors/{id}/offers` route is wired
    in this session's reduced API surface, so there was never a 422 to
    assert on; the "assertion" was smoke-testing that `issue_access_token`
    doesn't raise, nothing about the actual claim this test's name promised.

    Adding that endpoint is out of this session's scope (vendor-offer
    write endpoints sit alongside the order/billing surface another
    executor is rebuilding in parallel — see the executor manifest); the
    real, in-scope fix is a genuine assertion on what DOES exist here: the
    token this vendor principal receives round-trips correctly through
    `decode_access_token` AND through the live `get_principal` dependency
    (`GET /v1/auth/me`) — proving the auth layer, not just token issuance,
    actually resolves this VendorAdmin's vendor_id/roles/organisation_id
    correctly. Cases (1)-(4) above remain the ones that matter for the
    regulated-price rule itself (DB-level, structural)."""
    from rova.auth.security import decode_access_token, hash_password, issue_access_token
    from rova.core.ids import new_id

    session.execute(text(
        "INSERT INTO app_user (id, phone, name, password_hash) VALUES "
        "('usr_rp_vendor', '+258000999001', 'V', :h)"
    ), {"h": hash_password("x")})
    session.execute(text(
        "INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES "
        "('mem_rp_vendor', 'usr_rp_vendor', 'org_rp_test', ARRAY['VendorAdmin'])"
    ))
    session.commit()
    session_id = new_id("ses")
    token = issue_access_token(
        user_id="usr_rp_vendor", membership_id="mem_rp_vendor", organisation_id="org_rp_test",
        roles=["VendorAdmin"], surface="VN", session_id=session_id,
    )

    claims = decode_access_token(token)
    assert claims["sub"] == "usr_rp_vendor"
    assert claims["mid"] == "mem_rp_vendor"
    assert claims["org"] == "org_rp_test"
    assert claims["roles"] == ["VendorAdmin"]
    assert claims["surface"] == "VN"
    assert claims["sid"] == session_id

    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["user_id"] == "usr_rp_vendor"
    assert body["roles"] == ["VendorAdmin"]
    assert body["vendor_id"] == "ven_rp_test"  # resolved from organisation_id via vendor_account, not the JWT
    assert body["pharmacy_id"] is None
