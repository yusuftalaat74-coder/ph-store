"""Executor acceptance test (item 2 + item 3, backend-review-r1.md): one
order driven through HTTP only, end to end — login, request, checkout,
vendor accept (short on one line), reroute decision, pick with seals,
dispatch, delivery attempt, receipt, invoice upload, payment, and finally
CLOSED via the admin job-tick endpoint after the R-126 return window has
elapsed. Every step is asserted to have written its own `state_transition`
row, and credit exposure (pending + current, A3.3/A10) is checked to rise
after checkout and fall back down after the invoice is paid.

Fixtures are raw SQL (same pattern as `test_checkout_idempotency.py`) since
`conftest.py`'s `client` fixture only seeds config, not demo data — this
test owns its own vendor/pharmacy/products/credit_facility so it has no
dependency on `rova/seed/seed.py`'s (separately, manually) verified fixture.
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text

from rova.auth.security import hash_password
from rova.core.clock import freeze, unfreeze

SUFFIX = "flow1"


def _setup_fixture(db_engine, suffix: str) -> dict:
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"('org_flow_v_{suffix}', '77{suffix}1', 'Vendor Flow', 'vendor flow', 'VENDOR'), "
            f"('org_flow_p_{suffix}', '77{suffix}2', 'Pharmacy Flow', 'pharmacy flow', 'PHARMACY')"
        ))
        conn.execute(text(
            "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
            "delivery_mode, mov_amount, acceptance_mode, status) VALUES "
            f"('ven_flow_{suffix}', 'org_flow_v_{suffix}', 'MAPUTO_CIDADE', 'Vendor Flow', 'IMPORTER_WHOLESALER', "
            "'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE')"
        ))
        conn.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, "
            "address, latitude, longitude, status) VALUES "
            f"('pha_flow_{suffix}', 'org_flow_p_{suffix}', 'MAPUTO_CIDADE', 'A', 'Pharmacy Flow', 'X', 0, 0, 'ACTIVE')"
        ))
        for tag in ("a", "b"):
            conn.execute(text(
                "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
                "regulated_price, reviewer_ref, search_text) VALUES "
                f"('idx_flow_{suffix}_{tag}', 'Drug {tag}', 'c', '1', '1', 'M', 'AUTHORISED', false, "
                f"'LOCAL:{tag}', 'd')"
            ))
            conn.execute(text(
                "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
                "pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES "
                f"('ofr_flow_{suffix}_{tag}', 'ven_flow_{suffix}', 'idx_flow_{suffix}_{tag}', false, 100, '1', "
                "300, 20.00, now())"
            ))
        conn.execute(text(
            "INSERT INTO credit_facility (id, vendor_id, pharmacy_id, limit_amount, opening_balance, terms_days, "
            "status) VALUES "
            f"('crf_flow_{suffix}', 'ven_flow_{suffix}', 'pha_flow_{suffix}', 1000.00, 0, 30, 'ACTIVE')"
        ))

        def _user(uid, phone, name, org_id, roles):
            conn.execute(text(
                "INSERT INTO app_user (id, phone, name, password_hash) VALUES (:id, :phone, :name, :h)"
            ), {"id": uid, "phone": phone, "name": name, "h": hash_password("rova-demo")})
            conn.execute(text(
                "INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES "
                "(:mid, :uid, :org, :roles)"
            ), {"mid": f"mem_{uid}", "uid": uid, "org": org_id, "roles": roles})

        _user(f"usr_ph_{suffix}", f"+25884001{suffix}1", "Pharmacy User", f"org_flow_p_{suffix}",
              ["PharmacyBuyer", "PharmacyReceiver"])
        _user(f"usr_vn_{suffix}", f"+25884001{suffix}2", "Vendor User", f"org_flow_v_{suffix}", ["VendorAdmin"])
        _user(f"usr_cr_{suffix}", f"+25884001{suffix}3", "Courier User", f"org_flow_v_{suffix}", ["Courier"])
        _user(f"usr_pa_{suffix}", f"+25884001{suffix}4", "Platform Admin", None, ["PlatformAdmin"])

    return {
        "pharmacy_phone": f"+25884001{suffix}1",
        "vendor_phone": f"+25884001{suffix}2",
        "courier_phone": f"+25884001{suffix}3",
        "admin_phone": f"+25884001{suffix}4",
        "vendor_id": f"ven_flow_{suffix}",
        "pharmacy_id": f"pha_flow_{suffix}",
        "facility_id": f"crf_flow_{suffix}",
        "product_a": f"idx_flow_{suffix}_a",
        "product_b": f"idx_flow_{suffix}_b",
    }


def _login(client, phone):
    r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": "PH"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _transitions(db_engine, subject_id):
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT machine, trigger, to_state FROM state_transition WHERE subject_id=:s ORDER BY occurred_at"),
            {"s": subject_id},
        ).mappings().all()
    return [(r["machine"], r["trigger"], r["to_state"]) for r in rows]


def _exposure(db_engine, facility_id, vendor_id, pharmacy_id) -> Decimal:
    from rova.credit.exposure import current_exposure, pending_exposure
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=db_engine, future=True)
    s = Session()
    try:
        return current_exposure(s, facility_id) + pending_exposure(s, vendor_id, pharmacy_id)
    finally:
        s.close()


def test_full_order_loop_over_http(client, db_engine):
    fx = _setup_fixture(db_engine, SUFFIX)
    ph_headers = _login(client, fx["pharmacy_phone"])
    vn_headers = _login(client, fx["vendor_phone"])
    cr_headers = _login(client, fx["courier_phone"])
    pa_headers = _login(client, fx["admin_phone"])

    try:
        exposure_baseline = _exposure(db_engine, fx["facility_id"], fx["vendor_id"], fx["pharmacy_id"])
        assert exposure_baseline == Decimal("0.00")

        # --- request + lines -------------------------------------------------
        r = client.post("/v1/requests", json={"mode": "CATALOGUE", "allocation_strategy": "FEWEST_VENDORS"},
                         headers=ph_headers)
        assert r.status_code == 200, r.text
        request_id = r.json()["id"]

        r = client.post(f"/v1/requests/{request_id}/lines",
                         json={"index_product_id": fx["product_a"], "qty_requested": 4}, headers=ph_headers)
        assert r.status_code == 200, r.text
        r = client.post(f"/v1/requests/{request_id}/lines",
                         json={"index_product_id": fx["product_b"], "qty_requested": 3}, headers=ph_headers)
        assert r.status_code == 200, r.text

        # --- checkout (SM-01 SUBMIT_CART, credit terms) -----------------------
        r = client.post(f"/v1/requests/{request_id}/checkout",
                         headers={**ph_headers, "Idempotency-Key": f"checkout-{SUFFIX}"})
        assert r.status_code == 200, r.text
        checkout_body = r.json()
        assert len(checkout_body["orders"]) == 1
        order = checkout_body["orders"][0]
        order_id = order["id"]
        assert order["payment_terms"] == "CREDIT_N_DAYS"

        exposure_after_checkout = _exposure(db_engine, fx["facility_id"], fx["vendor_id"], fx["pharmacy_id"])
        assert exposure_after_checkout > exposure_baseline, "pending_exposure must rise once the order exists"

        with db_engine.connect() as conn:
            lines = conn.execute(
                text("SELECT id, index_product_id, ordered_qty, unit_price FROM order_line WHERE order_id=:o "
                     "ORDER BY index_product_id"),
                {"o": order_id},
            ).mappings().all()
        assert len(lines) == 2
        line_a = next(l for l in lines if l["index_product_id"] == fx["product_a"])
        line_b = next(l for l in lines if l["index_product_id"] == fx["product_b"])

        # --- vendor accept: line A full, line B short (1 of 3) ----------------
        r = client.post(
            f"/v1/orders/{order_id}/accept",
            json={"lines": [
                {"order_line_id": line_a["id"], "confirmed_qty": line_a["ordered_qty"]},
                {"order_line_id": line_b["id"], "confirmed_qty": 1},
            ]},
            headers=vn_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ACCEPTED"

        # --- reroute decision on the short line (DROP the remainder) ----------
        r = client.post(f"/v1/order-lines/{line_b['id']}/reroute-decision", json={"action": "DROP"},
                         headers=ph_headers)
        assert r.status_code == 200, r.text
        assert r.json()["fulfilment_status"] == "LINE_DROPPED"

        # --- pick both lines with seals -----------------------------------
        expiry = (date.today() + timedelta(days=200)).isoformat()
        for line_id, batch in ((line_a["id"], "BATCH-A"), (line_b["id"], "BATCH-B")):
            r = client.post(
                f"/v1/order-lines/{line_id}/pick",
                json={"batch_number": batch, "lot_number": "LOT-1", "expiry_date": expiry,
                      "seal_ids": [f"SEAL-{batch}"]},
                headers=vn_headers,
            )
            assert r.status_code == 200, r.text

        # --- dispatch -----------------------------------------------------
        r = client.post(f"/v1/orders/{order_id}/dispatch", headers=vn_headers)
        assert r.status_code == 200, r.text
        dispatched = r.json()
        assert dispatched["status"] == "DISPATCHED"
        job_id = dispatched["delivery_job"]["id"]

        # --- delivery job: assign, start, deliver --------------------------
        me = client.get("/v1/auth/me", headers=cr_headers).json()
        courier_user_id = me["user_id"]
        r = client.post(f"/v1/delivery-jobs/{job_id}/assign", json={"courier_user_id": courier_user_id},
                         headers=vn_headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ASSIGNED"

        r = client.post(f"/v1/delivery-jobs/{job_id}/start", headers=cr_headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "IN_TRANSIT"

        r = client.post(f"/v1/delivery-jobs/{job_id}/attempts",
                         json={"outcome": "DELIVERED", "proof_signature_or_code": "SIGNED-1"}, headers=cr_headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "DELIVERED"

        with db_engine.connect() as conn:
            order_status = conn.execute(text('SELECT status FROM "order" WHERE id=:o'), {"o": order_id}).scalar()
        assert order_status == "DELIVERED_PENDING_RECEIPT"

        # --- receipt (accept in full, no dispute) --------------------------
        r = client.post(
            f"/v1/orders/{order_id}/receipt",
            json={"lines": [
                {"order_line_id": line_a["id"], "accepted_qty": line_a["ordered_qty"], "rejected_qty": 0},
                {"order_line_id": line_b["id"], "accepted_qty": 1, "rejected_qty": 0},
            ]},
            headers=ph_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "RECEIPT_ACCEPTED"

        # --- invoice upload (prices match exactly -> auto MATCH_OK + FINALISE)
        r = client.post(
            f"/v1/orders/{order_id}/invoice",
            json={
                "vendor_invoice_number": f"INV-{SUFFIX}-1",
                "lines": [
                    {"order_line_id": line_a["id"], "invoiced_price": str(line_a["unit_price"]),
                     "invoiced_qty": line_a["ordered_qty"]},
                    {"order_line_id": line_b["id"], "invoiced_price": str(line_b["unit_price"]), "invoiced_qty": 1},
                ],
            },
            headers=vn_headers,
        )
        assert r.status_code == 200, r.text
        invoice = r.json()
        assert invoice["status"] == "AWAITING_PAYMENT"
        assert invoice["price_match_flag"] == "OK"
        invoice_id = invoice["id"]
        total_amount = Decimal(invoice["total_amount"])

        exposure_after_invoice = _exposure(db_engine, fx["facility_id"], fx["vendor_id"], fx["pharmacy_id"])
        assert exposure_after_invoice > Decimal("0.00"), \
            "current_exposure must reflect the finalised invoice before payment"

        # --- payment: full allocation --------------------------------------
        r = client.post(
            "/v1/payments",
            json={
                "pharmacy_id": fx["pharmacy_id"], "vendor_id": fx["vendor_id"], "method": "BANK_TRANSFER",
                "amount": str(total_amount), "collected_by": "VENDOR_DIRECT",
                "allocations": [{"invoice_id": invoice_id, "amount": str(total_amount)}],
            },
            headers=vn_headers,
        )
        assert r.status_code == 200, r.text

        with db_engine.connect() as conn:
            invoice_status = conn.execute(text("SELECT status FROM invoice WHERE id=:i"), {"i": invoice_id}).scalar()
        assert invoice_status == "PAID"

        exposure_after_payment = _exposure(db_engine, fx["facility_id"], fx["vendor_id"], fx["pharmacy_id"])
        assert exposure_after_payment < exposure_after_invoice, "exposure must fall once the invoice is paid"
        assert exposure_after_payment == Decimal("0.00")

        # --- R-126: freeze the clock past the return window, then close ----
        with db_engine.connect() as conn:
            receipt_at = conn.execute(
                text("SELECT occurred_at FROM state_transition WHERE machine='SM-03' AND subject_id=:o "
                     "AND to_state='RECEIPT_ACCEPTED' ORDER BY occurred_at DESC LIMIT 1"),
                {"o": order_id},
            ).scalar()
        freeze(receipt_at + timedelta(days=8))

        r = client.post("/v1/admin/jobs/tick", json={"job": "close_orders"}, headers=pa_headers)
        assert r.status_code == 200, r.text
        assert r.json()["fired"] == 1

        with db_engine.connect() as conn:
            final_status = conn.execute(text('SELECT status FROM "order" WHERE id=:o'), {"o": order_id}).scalar()
        assert final_status == "CLOSED"

        # --- every step left its own state_transition row -------------------
        order_transitions = _transitions(db_engine, order_id)
        order_triggers = [t[1] for t in order_transitions]
        for expected in ("ACCEPT", "DISPATCH", "DELIVERED", "RECEIPT_ACCEPT", "CLOSE"):
            assert expected in order_triggers, f"missing SM-03 {expected} in {order_triggers}"

        line_a_triggers = [t[1] for t in _transitions(db_engine, line_a["id"])]
        assert "CONFIRM_FULL" in line_a_triggers

        line_b_triggers = [t[1] for t in _transitions(db_engine, line_b["id"])]
        assert "CONFIRM_SHORT" in line_b_triggers
        assert "DROP" in line_b_triggers

        job_triggers = [t[1] for t in _transitions(db_engine, job_id)]
        for expected in ("ASSIGN", "START", "ATTEMPT_DELIVERED"):
            assert expected in job_triggers, f"missing SM-05 {expected} in {job_triggers}"

        invoice_triggers = [t[1] for t in _transitions(db_engine, invoice_id)]
        for expected in ("MATCH_OK", "FINALISE", "ALLOCATE_FULL"):
            assert expected in invoice_triggers, f"missing SM-11 {expected} in {invoice_triggers}"
    finally:
        unfreeze()
