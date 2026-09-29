"""A17 'Checkout idempotency' row / B3.22 — same Idempotency-Key twice must
not create two orders (executor manifest, "where the architect will look
hardest")."""
from decimal import Decimal

from sqlalchemy import text

from rova.auth.security import hash_password
from rova.core.idempotency import check_and_store, store


def _setup_fixture(db_engine, suffix: str):
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"('org_idem_v_{suffix}', '95{suffix}1', 'V', 'v', 'VENDOR'), "
            f"('org_idem_p_{suffix}', '95{suffix}2', 'P', 'p', 'PHARMACY')"
        ))
        conn.execute(text(
            "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
            "delivery_mode, mov_amount, acceptance_mode, status) VALUES "
            f"('ven_idem_{suffix}', 'org_idem_v_{suffix}', 'MAPUTO_CIDADE', 'V', 'IMPORTER_WHOLESALER', "
            "'VENDOR_OWN_FLEET', 0, 'AUTO_ACCEPT_FULL', 'ACTIVE')"
        ))
        conn.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, "
            "address, latitude, longitude, status) VALUES "
            f"('pha_idem_{suffix}', 'org_idem_p_{suffix}', 'MAPUTO_CIDADE', 'A', 'P', 'X', 0, 0, 'ACTIVE')"
        ))
        conn.execute(text(
            "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
            "regulated_price, reviewer_ref, search_text) VALUES "
            f"('idx_idem_{suffix}', 'D', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x', 'd')"
        ))
        conn.execute(text(
            "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
            "pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES "
            f"('ofr_idem_{suffix}', 'ven_idem_{suffix}', 'idx_idem_{suffix}', false, 100, '1', 300, 15.00, now())"
        ))
        conn.execute(text(
            f"INSERT INTO app_user (id, phone, name, password_hash) VALUES "
            f"('usr_idem_{suffix}', :phone, 'Buyer', :h)"
        ), {"phone": f"+2588400009{suffix}", "h": hash_password("rova-demo")})
        conn.execute(text(
            f"INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES "
            f"('mem_idem_{suffix}', 'usr_idem_{suffix}', 'org_idem_p_{suffix}', ARRAY['PharmacyBuyer'])"
        ))


def _login(client, phone):
    r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": "PH"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_same_key_twice_creates_one_order_and_replays(client, db_engine):
    _setup_fixture(db_engine, "a1")
    token = _login(client, "+2588400009a1")
    headers = {"Authorization": f"Bearer {token}"}

    r = client.post("/v1/requests", json={"mode": "CATALOGUE", "allocation_strategy": "FEWEST_VENDORS"}, headers=headers)
    assert r.status_code == 200
    request_id = r.json()["id"]

    r = client.post(f"/v1/requests/{request_id}/lines",
                     json={"index_product_id": "idx_idem_a1", "qty_requested": 2}, headers=headers)
    assert r.status_code == 200

    r_missing = client.post(f"/v1/requests/{request_id}/checkout", headers=headers)
    assert r_missing.status_code == 422

    key_headers = {**headers, "Idempotency-Key": "test-idem-key-a1"}
    r1 = client.post(f"/v1/requests/{request_id}/checkout", headers=key_headers)
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/v1/requests/{request_id}/checkout", headers=key_headers)
    assert r2.status_code == 200
    assert r1.json() == r2.json()

    with db_engine.connect() as conn:
        count = conn.execute(text('SELECT count(*) FROM "order" WHERE request_id=:r'), {"r": request_id}).scalar()
    assert count == 1


def test_same_key_different_path_conflicts(client, db_engine):
    """Renamed from the old `test_same_key_different_body_conflicts`: this
    varies the URL path (two different request_ids), not the request BODY
    — `POST /v1/requests/{id}/checkout` has no body model at all, so no
    HTTP call to it can ever carry a genuinely different body (see
    `rova/core/idempotency.py`'s module docstring and
    `test_same_key_different_body_conflicts` below, which is item 1c/5's
    real fix). This is still a legitimate, separate guarantee worth keeping
    — the same key reused against a different resource must conflict too
    — just honestly named for what it actually exercises."""
    _setup_fixture(db_engine, "a2")
    token = _login(client, "+2588400009a2")
    headers = {"Authorization": f"Bearer {token}"}

    r = client.post("/v1/requests", json={"mode": "CATALOGUE"}, headers=headers)
    req1 = r.json()["id"]
    r = client.post("/v1/requests", json={"mode": "CATALOGUE"}, headers=headers)
    req2 = r.json()["id"]
    for rid in (req1, req2):
        client.post(f"/v1/requests/{rid}/lines", json={"index_product_id": "idx_idem_a2", "qty_requested": 1},
                     headers=headers)

    key_headers = {**headers, "Idempotency-Key": "shared-key"}
    r1 = client.post(f"/v1/requests/{req1}/checkout", headers=key_headers)
    assert r1.status_code == 200
    r2 = client.post(f"/v1/requests/{req2}/checkout", headers=key_headers)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_same_key_different_body_conflicts(session):
    """B fix 1c / item 5's real fix for this test name: a REAL difference in
    request body content (not the URL path) on the same Idempotency-Key
    must 409, proven directly against `rova.core.idempotency` — see that
    module's own docstring and `tests/domain/test_idempotency.py` for why
    this can't be driven through the checkout HTTP endpoint itself (it has
    no body model; every call there passes `body={}}`, so the URL path is
    the only thing that can differ over HTTP today) without touching
    `rova/ordering/router.py`, which is out of this session's scope."""
    from rova.core.errors import ApiError

    key, principal_id, path = "shared-key-real-body", "principal-checkout-idem", "/v1/requests/req1/checkout"
    first_body = {"cart_note": "urgent"}
    different_body = {"cart_note": "not urgent"}

    assert check_and_store(session, key=key, principal_id=principal_id, method="POST", path=path,
                            body=first_body) is None
    store(session, key=key, principal_id=principal_id, method="POST", path=path, body=first_body,
          status_code=200, response_body={"request_id": "req1", "orders": []})
    session.commit()

    try:
        check_and_store(session, key=key, principal_id=principal_id, method="POST", path=path, body=different_body)
    except ApiError as exc:
        assert exc.code == "IDEMPOTENCY_CONFLICT"
        assert exc.status_code == 409
    else:
        raise AssertionError("expected IDEMPOTENCY_CONFLICT for a genuinely different body on the same key")
