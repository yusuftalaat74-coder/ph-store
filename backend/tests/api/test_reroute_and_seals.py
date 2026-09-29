"""Executor acceptance tests for defect 4 (rerouted remainder orphaned) and
defect 6 (duplicate seal 500) — driven over HTTP, matching how the
architect found both."""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text

from rova.auth.security import hash_password


def _login(client, phone):
    r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": "PH"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _base_pharmacy_and_product(conn, suffix: str):
    conn.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        f"('org_rs_p_{suffix}', '96{suffix}1', 'P', 'p', 'PHARMACY')"
    ))
    conn.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude, status) VALUES "
        f"('pha_rs_{suffix}', 'org_rs_p_{suffix}', 'MAPUTO_CIDADE', 'A', 'P', 'X', 0, 0, 'ACTIVE')"
    ))
    conn.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        f"('idx_rs_{suffix}', 'D', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x', 'd')"
    ))
    conn.execute(text(
        "INSERT INTO app_user (id, phone, name, password_hash) VALUES (:id, :phone, 'Buyer', :h)"
    ), {"id": f"usr_rs_{suffix}", "phone": f"+2588400008{suffix}", "h": hash_password("rova-demo")})
    conn.execute(text(
        "INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES "
        f"('mem_rs_{suffix}', 'usr_rs_{suffix}', 'org_rs_p_{suffix}', ARRAY['PharmacyBuyer'])"
    ))
    return f"+2588400008{suffix}"


def _vendor(conn, suffix: str, tag: str, price: str, qty: int = 100):
    conn.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        f"('org_rs_v{tag}_{suffix}', '97{suffix}{tag}', 'V{tag}', 'v{tag}', 'VENDOR')"
    ))
    conn.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount, acceptance_mode, status) VALUES "
        f"('ven_rs_{tag}_{suffix}', 'org_rs_v{tag}_{suffix}', 'MAPUTO_CIDADE', 'V{tag}', 'IMPORTER_WHOLESALER', "
        "'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE')"
    ))
    conn.execute(text(
        "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, pack_size, "
        "expiry_horizon_days, price, stock_confirmed_at) VALUES "
        f"('ofr_rs_{tag}_{suffix}', 'ven_rs_{tag}_{suffix}', 'idx_rs_{suffix}', false, :qty, '1', 300, :price, now())"
    ), {"qty": qty, "price": Decimal(price)})
    conn.execute(text(
        "INSERT INTO app_user (id, phone, name, password_hash) VALUES (:id, :phone, :name, :h)"
    ), {"id": f"usr_rs_v{tag}_{suffix}", "phone": f"+2588400007{tag}{suffix}", "name": f"Vendor {tag}",
        "h": hash_password("rova-demo")})
    conn.execute(text(
        "INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES "
        f"('mem_rs_v{tag}_{suffix}', 'usr_rs_v{tag}_{suffix}', 'org_rs_v{tag}_{suffix}', ARRAY['VendorOrderDesk'])"
    ))
    return f"ven_rs_{tag}_{suffix}", f"+2588400007{tag}{suffix}"


def test_reroute_remainder_gets_allocated_excluding_origin_vendor(client, db_engine):
    """Defect 4. Vendor A (cheaper, so ranking picks it first at checkout)
    confirms only part of the order -> LINE_SHORT. The pharmacy asks to
    REROUTE the remainder; before the fix this created a `request_line` with
    `origin_line_id` set and nothing else — zero `allocation` rows, no
    second `order`. After the fix, `POST /v1/request-lines/{id}/allocate`
    ranks the remainder (Vendor A excluded, R-029) and creates a follow-on
    order with Vendor B, the only other fresh candidate."""
    suffix = "rr1"
    with db_engine.begin() as conn:
        phone = _base_pharmacy_and_product(conn, suffix)
        vendor_a, _ = _vendor(conn, suffix, "a", "10.00")
        vendor_b, _ = _vendor(conn, suffix, "b", "20.00")

    headers = _login(client, phone)

    r = client.post("/v1/requests", json={"mode": "CATALOGUE", "allocation_strategy": "FEWEST_VENDORS"},
                     headers=headers)
    assert r.status_code == 200, r.text
    request_id = r.json()["id"]

    r = client.post(f"/v1/requests/{request_id}/lines",
                     json={"index_product_id": f"idx_rs_{suffix}", "qty_requested": 10}, headers=headers)
    assert r.status_code == 200, r.text

    r = client.post(f"/v1/requests/{request_id}/checkout",
                     headers={**headers, "Idempotency-Key": f"co-{suffix}"})
    assert r.status_code == 200, r.text
    order = r.json()["orders"][0]
    assert order["vendor_id"] == vendor_a
    order_id = order["id"]

    with db_engine.connect() as conn:
        line = conn.execute(
            text("SELECT id, request_line_id FROM order_line WHERE order_id=:o"), {"o": order_id}
        ).mappings().one()

    vendor_a_headers = _login(client, f"+2588400007a{suffix}")
    r = client.post(f"/v1/orders/{order_id}/accept",
                     json={"lines": [{"order_line_id": line["id"], "confirmed_qty": 4}]}, headers=vendor_a_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ACCEPTED"

    r = client.post(f"/v1/order-lines/{line['id']}/reroute-decision", json={"action": "REROUTE"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["fulfilment_status"] == "LINE_REROUTED"

    with db_engine.connect() as conn:
        remainder = conn.execute(
            text("SELECT id, qty_requested, match_status FROM request_line WHERE origin_line_id=:o"),
            {"o": line["request_line_id"]},
        ).mappings().one()
    assert remainder["qty_requested"] == 6  # 10 ordered - 4 confirmed
    assert remainder["match_status"] == "UNRESOLVED"  # not yet allocated (defect 4)

    r = client.post(f"/v1/request-lines/{remainder['id']}/allocate", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match_status"] == "RESOLVED"
    assert body["order"] is not None
    assert body["order"]["vendor_id"] == vendor_b  # vendor A excluded, R-029

    with db_engine.connect() as conn:
        alloc = conn.execute(
            text("SELECT vendor_id FROM allocation WHERE request_line_id=:l"), {"l": remainder["id"]}
        ).mappings().one()
        new_order = conn.execute(
            text("SELECT vendor_id, status FROM \"order\" WHERE id=:o"), {"o": body["order"]["id"]}
        ).mappings().one()
    assert alloc["vendor_id"] == vendor_b
    assert new_order["vendor_id"] == vendor_b
    assert new_order["status"] == "PENDING_ACCEPTANCE"

    # calling it again is refused, not silently re-allocated
    r = client.post(f"/v1/request-lines/{remainder['id']}/allocate", headers=headers)
    assert r.status_code == 409, r.text


def test_pick_rejects_seal_already_dispatched_elsewhere(client, db_engine):
    """Defect 6. `uq_traceability_seal_dispatched` (R-057: a seal is
    dispatched once) used to only be discovered at `dispatch` time, as a bare
    IntegrityError -> 500 INTERNAL. Picking a line with a seal that some
    other order already dispatched must 4xx at pick time instead, naming the
    duplicate seal."""
    suffix = "ds1"
    with db_engine.begin() as conn:
        phone = _base_pharmacy_and_product(conn, suffix)
        vendor_a, _ = _vendor(conn, suffix, "a", "10.00")

    headers = _login(client, phone)
    vendor_headers = _login(client, f"+2588400007a{suffix}")

    def _order_with_confirmed_line(product_qty: int = 3):
        r = client.post("/v1/requests", json={"mode": "CATALOGUE", "allocation_strategy": "FEWEST_VENDORS"},
                         headers=headers)
        request_id = r.json()["id"]
        client.post(f"/v1/requests/{request_id}/lines",
                    json={"index_product_id": f"idx_rs_{suffix}", "qty_requested": product_qty}, headers=headers)
        r = client.post(f"/v1/requests/{request_id}/checkout",
                         headers={**headers, "Idempotency-Key": f"co-{suffix}-{request_id}"})
        order = r.json()["orders"][0]
        with db_engine.connect() as conn:
            line = conn.execute(
                text("SELECT id FROM order_line WHERE order_id=:o"), {"o": order["id"]}
            ).mappings().one()
        r = client.post(f"/v1/orders/{order['id']}/accept",
                         json={"lines": [{"order_line_id": line["id"], "confirmed_qty": product_qty}]},
                         headers=vendor_headers)
        assert r.status_code == 200, r.text
        return order["id"], line["id"]

    order1_id, line1_id = _order_with_confirmed_line()
    expiry = (date.today() + timedelta(days=200)).isoformat()

    r = client.post(f"/v1/order-lines/{line1_id}/pick",
                     json={"batch_number": "B1", "lot_number": "L1", "expiry_date": expiry, "seal_ids": ["SEAL-DUP"]},
                     headers=vendor_headers)
    assert r.status_code == 200, r.text
    r = client.post(f"/v1/orders/{order1_id}/dispatch", headers=vendor_headers)
    assert r.status_code == 200, r.text  # SEAL-DUP is now traceability_event(DISPATCHED)

    order2_id, line2_id = _order_with_confirmed_line()
    r = client.post(f"/v1/order-lines/{line2_id}/pick",
                     json={"batch_number": "B2", "lot_number": "L2", "expiry_date": expiry, "seal_ids": ["SEAL-DUP"]},
                     headers=vendor_headers)
    assert r.status_code in (409, 422), r.text
    assert "SEAL-DUP" in r.text


def test_pick_rejects_seal_duplicated_within_same_order(client, db_engine):
    """Defect 6, same-order case: two lines on one still-undispatched order
    must not be pickable with the same seal id either — that would 500 at
    dispatch time within a single DISPATCH transaction, before either seal
    row is individually committed."""
    suffix = "ds2"
    with db_engine.begin() as conn:
        phone = _base_pharmacy_and_product(conn, suffix)
        vendor_a, _ = _vendor(conn, suffix, "a", "10.00")
        conn.execute(text(
            "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
            "regulated_price, reviewer_ref, search_text) VALUES "
            f"('idx_rs_{suffix}_2', 'D2', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x2', 'd2')"
        ))
        conn.execute(text(
            "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
            "pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES "
            f"('ofr_rs_a2_{suffix}', 'ven_rs_a_{suffix}', 'idx_rs_{suffix}_2', false, 100, '1', 300, 10.00, now())"
        ))

    headers = _login(client, phone)
    vendor_headers = _login(client, f"+2588400007a{suffix}")

    r = client.post("/v1/requests", json={"mode": "CATALOGUE", "allocation_strategy": "FEWEST_VENDORS"},
                     headers=headers)
    request_id = r.json()["id"]
    client.post(f"/v1/requests/{request_id}/lines",
                json={"index_product_id": f"idx_rs_{suffix}", "qty_requested": 2}, headers=headers)
    client.post(f"/v1/requests/{request_id}/lines",
                json={"index_product_id": f"idx_rs_{suffix}_2", "qty_requested": 2}, headers=headers)
    r = client.post(f"/v1/requests/{request_id}/checkout",
                     headers={**headers, "Idempotency-Key": f"co-{suffix}"})
    order = r.json()["orders"][0]
    with db_engine.connect() as conn:
        lines = conn.execute(
            text("SELECT id FROM order_line WHERE order_id=:o ORDER BY id"), {"o": order["id"]}
        ).mappings().all()
    assert len(lines) == 2
    r = client.post(f"/v1/orders/{order['id']}/accept",
                     json={"lines": [{"order_line_id": lines[0]["id"], "confirmed_qty": 2},
                                      {"order_line_id": lines[1]["id"], "confirmed_qty": 2}]},
                     headers=vendor_headers)
    assert r.status_code == 200, r.text

    expiry = (date.today() + timedelta(days=200)).isoformat()
    r = client.post(f"/v1/order-lines/{lines[0]['id']}/pick",
                     json={"batch_number": "B1", "lot_number": "L1", "expiry_date": expiry,
                           "seal_ids": ["SEAL-SAME"]}, headers=vendor_headers)
    assert r.status_code == 200, r.text

    r = client.post(f"/v1/order-lines/{lines[1]['id']}/pick",
                     json={"batch_number": "B2", "lot_number": "L2", "expiry_date": expiry,
                           "seal_ids": ["SEAL-SAME"]}, headers=vendor_headers)
    assert r.status_code in (409, 422), r.text
    assert "SEAL-SAME" in r.text
