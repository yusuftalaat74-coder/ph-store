"""Disputes, returns, credit notes and the credit desk, over HTTP.

Each test drives a real order to the state its scenario needs — nothing is
faked into a table directly except the fixture accounts, because the value of
these endpoints is precisely that they sit on top of the real money path.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from rova.auth.security import hash_password


def _fixture(db_engine, suffix: str, *, limit: str = "100000.00", terms_days: int = 30) -> dict:
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"('org_rv_{suffix}', 'TXRV{suffix}1', 'V', 'v', 'VENDOR'), "
            f"('org_rp_{suffix}', 'TXRV{suffix}2', 'P', 'p', 'PHARMACY')"))
        conn.execute(text(
            "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
            "delivery_mode, mov_amount, acceptance_mode, status) VALUES "
            f"('ven_rv_{suffix}', 'org_rv_{suffix}', 'MAPUTO_CIDADE', 'V', 'IMPORTER_WHOLESALER', "
            "'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE')"))
        conn.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, "
            "address, latitude, longitude, status) VALUES "
            f"('pha_rp_{suffix}', 'org_rp_{suffix}', 'MAPUTO_CIDADE', 'A', 'P', 'X', 0, 0, 'ACTIVE')"))
        conn.execute(text(
            "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
            "regulated_price, reviewer_ref, search_text) VALUES "
            f"('idx_rv_{suffix}', 'Drug', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x', 'd')"))
        conn.execute(text(
            "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
            "pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES "
            f"('ofr_rv_{suffix}', 'ven_rv_{suffix}', 'idx_rv_{suffix}', false, 500, '1', 300, 20.00, now())"))
        conn.execute(text(
            "INSERT INTO credit_facility (id, vendor_id, pharmacy_id, limit_amount, opening_balance, "
            "terms_days, status) VALUES "
            f"('crf_rv_{suffix}', 'ven_rv_{suffix}', 'pha_rp_{suffix}', {limit}, 0, {terms_days}, 'ACTIVE')"))

        def _user(uid, phone, org, roles):
            conn.execute(text("INSERT INTO app_user (id, phone, name, password_hash) "
                              "VALUES (:id, :p, 'U', :h)"),
                         {"id": uid, "p": phone, "h": hash_password("rova-demo")})
            conn.execute(text("INSERT INTO membership (id, user_id, organisation_id, role_codes) "
                              "VALUES (:m, :u, :o, :r)"),
                         {"m": f"mem_{uid}", "u": uid, "o": org, "r": roles})

        _user(f"usr_rph_{suffix}", f"+2588455{suffix}1", f"org_rp_{suffix}",
              ["PharmacyAdmin", "PharmacyBuyer", "PharmacyReceiver"])
        _user(f"usr_rvn_{suffix}", f"+2588455{suffix}2", f"org_rv_{suffix}",
              ["VendorAdmin", "VendorFinance"])
        _user(f"usr_rcr_{suffix}", f"+2588455{suffix}3", f"org_rv_{suffix}", ["Courier", "Dispatcher"])
        _user(f"usr_rcf_{suffix}", f"+2588455{suffix}4", None, ["ComplianceOfficer"])
        _user(f"usr_rpf_{suffix}", f"+2588455{suffix}5", None, ["PlatformFinance"])

    return {"suffix": suffix, "vendor_id": f"ven_rv_{suffix}", "pharmacy_id": f"pha_rp_{suffix}",
            "facility_id": f"crf_rv_{suffix}", "product": f"idx_rv_{suffix}",
            "ph": f"+2588455{suffix}1", "vn": f"+2588455{suffix}2", "cr": f"+2588455{suffix}3",
            "cf": f"+2588455{suffix}4", "pf": f"+2588455{suffix}5"}


def _h(client, phone, surface="PH"):
    r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": surface})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _order_to_receipt_accepted(client, db_engine, fx, qty: int = 10) -> dict:
    """Drives one order all the way to RECEIPT_ACCEPTED over HTTP."""
    s = fx["suffix"]
    ph, vn, cr = _h(client, fx["ph"]), _h(client, fx["vn"], "VN"), _h(client, fx["cr"], "CR")

    rid = client.post("/v1/requests", headers=ph,
                      json={"mode": "CATALOGUE", "allocation_strategy": "FEWEST_VENDORS"}).json()["id"]
    client.post(f"/v1/requests/{rid}/lines", headers=ph,
                json={"index_product_id": fx["product"], "qty_requested": qty})
    out = client.post(f"/v1/requests/{rid}/checkout",
                      headers={**ph, "Idempotency-Key": f"co-{s}-{qty}"})
    assert out.status_code == 200, out.text
    order_id = out.json()["orders"][0]["id"]

    with db_engine.connect() as conn:
        line = conn.execute(text("SELECT id, ordered_qty, unit_price FROM order_line WHERE order_id=:o"),
                            {"o": order_id}).mappings().one()

    client.post(f"/v1/orders/{order_id}/accept", headers=vn,
                json={"lines": [{"order_line_id": line["id"], "confirmed_qty": line["ordered_qty"]}]})
    expiry = (date.today() + timedelta(days=200)).isoformat()
    # seal ids are globally unique (a seal is a physical object, R-064) — they
    # must differ per order or the second dispatch is refused.
    picked = client.post(f"/v1/order-lines/{line['id']}/pick", headers=vn,
                         json={"batch_number": f"B-{s}", "lot_number": f"L-{s}",
                               "expiry_date": expiry, "seal_ids": [f"SEAL-{s}-{qty}"]})
    assert picked.status_code == 200, picked.text
    dispatched = client.post(f"/v1/orders/{order_id}/dispatch", headers=vn)
    assert dispatched.status_code == 200, dispatched.text
    job_id = dispatched.json()["delivery_job"]["id"]
    courier_user_id = client.get("/v1/auth/me", headers=cr).json()["user_id"]
    client.post(f"/v1/delivery-jobs/{job_id}/assign", headers=vn, json={"courier_user_id": courier_user_id})
    client.post(f"/v1/delivery-jobs/{job_id}/start", headers=cr)
    client.post(f"/v1/delivery-jobs/{job_id}/attempts", headers=cr,
                json={"outcome": "DELIVERED", "proof_signature_or_code": "SIG"})
    rec = client.post(f"/v1/orders/{order_id}/receipt", headers=ph,
                      json={"lines": [{"order_line_id": line["id"],
                                       "accepted_qty": line["ordered_qty"], "rejected_qty": 0}]})
    assert rec.status_code == 200, rec.text

    with db_engine.connect() as conn:
        receipt_line_id = conn.execute(
            text("SELECT rl.id FROM receipt_line rl JOIN receipt r ON r.id = rl.receipt_id WHERE r.order_id=:o"),
            {"o": order_id},
        ).scalar()
    return {"order_id": order_id, "order_line_id": line["id"], "receipt_line_id": receipt_line_id,
            "unit_price": Decimal(line["unit_price"]), "qty": line["ordered_qty"]}


def _invoice(client, fx, ctx, number: str) -> dict:
    vn = _h(client, fx["vn"], "VN")
    r = client.post(f"/v1/orders/{ctx['order_id']}/invoice", headers=vn, json={
        "vendor_invoice_number": number,
        "lines": [{"order_line_id": ctx["order_line_id"], "invoiced_price": str(ctx["unit_price"]),
                   "invoiced_qty": ctx["qty"]}],
    })
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ disputes

def test_dispute_needs_exactly_one_subject(client, db_engine):
    fx = _fixture(db_engine, "d1")
    ctx = _order_to_receipt_accepted(client, db_engine, fx)
    ph = _h(client, fx["ph"])

    neither = client.post("/v1/disputes", headers=ph,
                          json={"order_id": ctx["order_id"], "type": "RECEIPT_DISCREPANCY"})
    assert neither.status_code == 422

    inv = _invoice(client, fx, ctx, "INV-d1")
    with db_engine.connect() as conn:
        invoice_line_id = conn.execute(text("SELECT id FROM invoice_line WHERE invoice_id=:i"),
                                       {"i": inv["id"]}).scalar()
    both = client.post("/v1/disputes", headers=ph, json={
        "order_id": ctx["order_id"], "type": "RECEIPT_DISCREPANCY",
        "receipt_line_id": ctx["receipt_line_id"], "invoice_line_id": invoice_line_id,
    })
    assert both.status_code == 422, "the XOR must be refused with a named field, not a CHECK violation"


def test_dispute_subject_must_belong_to_the_order(client, db_engine):
    fx_a = _fixture(db_engine, "d2")
    fx_b = _fixture(db_engine, "d3")
    ctx_a = _order_to_receipt_accepted(client, db_engine, fx_a)
    ctx_b = _order_to_receipt_accepted(client, db_engine, fx_b)
    ph = _h(client, fx_a["ph"])
    r = client.post("/v1/disputes", headers=ph, json={
        "order_id": ctx_a["order_id"], "type": "RECEIPT_DISCREPANCY",
        "receipt_line_id": ctx_b["receipt_line_id"],
    })
    assert r.status_code == 422


def test_dispute_assign_then_resolve(client, db_engine):
    fx = _fixture(db_engine, "d4")
    ctx = _order_to_receipt_accepted(client, db_engine, fx)
    ph, vn = _h(client, fx["ph"]), _h(client, fx["vn"], "VN")

    d = client.post("/v1/disputes", headers=ph, json={
        "order_id": ctx["order_id"], "type": "RECEIPT_DISCREPANCY",
        "receipt_line_id": ctx["receipt_line_id"], "notes": "faltaram 2 caixas",
    })
    assert d.status_code == 201, d.text
    dispute_id = d.json()["id"]
    assert d.json()["status"] == "OPEN"
    assert d.json()["raised_by"] == "PharmacyReceiver"

    assigned = client.post(f"/v1/disputes/{dispute_id}/assign", headers=vn)
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["status"] == "UNDER_REVIEW"
    assert assigned.json()["assigned_to_user_id"] is not None

    resolved = client.post(f"/v1/disputes/{dispute_id}/resolve", headers=vn,
                           json={"outcome": "CREDIT_NOTE", "notes": "aceite"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "RESOLVED"
    assert resolved.json()["outcome"] == "CREDIT_NOTE"

    view = client.get(f"/v1/disputes/{dispute_id}", headers=ph)
    assert [t["trigger"] for t in view.json()["transitions"]] == ["ASSIGN", "RESOLVE"]


def test_resolving_without_assignment_is_an_illegal_transition(client, db_engine):
    fx = _fixture(db_engine, "d5")
    ctx = _order_to_receipt_accepted(client, db_engine, fx)
    ph, vn = _h(client, fx["ph"]), _h(client, fx["vn"], "VN")
    dispute_id = client.post("/v1/disputes", headers=ph, json={
        "order_id": ctx["order_id"], "type": "RECEIPT_DISCREPANCY",
        "receipt_line_id": ctx["receipt_line_id"],
    }).json()["id"]
    r = client.post(f"/v1/disputes/{dispute_id}/resolve", headers=vn, json={"outcome": "REJECTED"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ILLEGAL_TRANSITION"


def test_only_compliance_may_resolve_a_regulated_price_incident(client, db_engine):
    """R-074, enforced by SM-09's own guard."""
    fx = _fixture(db_engine, "d6")
    ctx = _order_to_receipt_accepted(client, db_engine, fx)
    cf = _h(client, fx["cf"], "OP")
    vn = _h(client, fx["vn"], "VN")

    dispute_id = client.post("/v1/disputes", headers=cf, json={
        "order_id": ctx["order_id"], "type": "REGULATED_PRICE_INCIDENT",
        "receipt_line_id": ctx["receipt_line_id"],
    }).json()["id"]
    client.post(f"/v1/disputes/{dispute_id}/assign", headers=cf)

    by_vendor = client.post(f"/v1/disputes/{dispute_id}/resolve", headers=vn,
                            json={"outcome": "REJECTED"})
    assert by_vendor.status_code == 409
    assert by_vendor.json()["error"]["rule"] == "R-074"

    by_compliance = client.post(f"/v1/disputes/{dispute_id}/resolve", headers=cf,
                                json={"outcome": "ESCALATED_TO_COMPLIANCE"})
    assert by_compliance.status_code == 200, by_compliance.text

    with db_engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM audit_event WHERE subject_id=:d "
                              "AND action_code='PRICE_DEVIATION_RESOLUTION'"), {"d": dispute_id}).scalar()
    assert n == 1, "a regulated-price resolution must always be audited"


def test_a_pharmacy_cannot_see_another_pharmacys_dispute(client, db_engine):
    fx_a = _fixture(db_engine, "d7")
    fx_b = _fixture(db_engine, "d8")
    ctx = _order_to_receipt_accepted(client, db_engine, fx_a)
    ph_a, ph_b = _h(client, fx_a["ph"]), _h(client, fx_b["ph"])
    dispute_id = client.post("/v1/disputes", headers=ph_a, json={
        "order_id": ctx["order_id"], "type": "RECEIPT_DISCREPANCY",
        "receipt_line_id": ctx["receipt_line_id"],
    }).json()["id"]
    assert client.get(f"/v1/disputes/{dispute_id}", headers=ph_b).status_code == 404
    assert not any(x["id"] == dispute_id for x in client.get("/v1/disputes", headers=ph_b).json()["items"])


# ------------------------------------------------------------------ returns

def test_return_cannot_exceed_the_accepted_quantity(client, db_engine):
    fx = _fixture(db_engine, "r1")
    ctx = _order_to_receipt_accepted(client, db_engine, fx, qty=10)
    ph = _h(client, fx["ph"])
    r = client.post("/v1/returns", headers=ph, json={
        "order_id": ctx["order_id"],
        "lines": [{"order_line_id": ctx["order_line_id"], "qty": 11, "reason_code": "DAMAGED"}],
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GUARD_FAILED"


def test_two_returns_cannot_together_exceed_what_was_accepted(client, db_engine):
    fx = _fixture(db_engine, "r2")
    ctx = _order_to_receipt_accepted(client, db_engine, fx, qty=10)
    ph = _h(client, fx["ph"])
    body = {"order_id": ctx["order_id"],
            "lines": [{"order_line_id": ctx["order_line_id"], "qty": 6, "reason_code": "DAMAGED"}]}
    assert client.post("/v1/returns", headers=ph, json=body).status_code == 201
    second = client.post("/v1/returns", headers=ph, json=body)
    assert second.status_code == 409, "6 + 6 > 10 accepted"


def test_return_lifecycle_to_received(client, db_engine):
    fx = _fixture(db_engine, "r3")
    ctx = _order_to_receipt_accepted(client, db_engine, fx, qty=10)
    ph, vn, cr = _h(client, fx["ph"]), _h(client, fx["vn"], "VN"), _h(client, fx["cr"], "CR")

    r = client.post("/v1/returns", headers=ph, json={
        "order_id": ctx["order_id"],
        "lines": [{"order_line_id": ctx["order_line_id"], "qty": 3, "reason_code": "NEAR_EXPIRY"}],
    })
    assert r.status_code == 201, r.text
    return_id = r.json()["id"]
    assert r.json()["rma_number"].startswith("RMA-")
    assert len(r.json()["lines"]) == 1

    assert client.post(f"/v1/returns/{return_id}/approve", headers=vn).json()["status"] == "APPROVED"
    assert client.post(f"/v1/returns/{return_id}/ship", headers=cr).json()["status"] == "GOODS_IN_TRANSIT"
    assert client.post(f"/v1/returns/{return_id}/receive", headers=vn).json()["status"] == "RECEIVED_BY_VENDOR"


def test_a_rejected_return_frees_the_quantity_again(client, db_engine):
    fx = _fixture(db_engine, "r4")
    ctx = _order_to_receipt_accepted(client, db_engine, fx, qty=5)
    ph, vn = _h(client, fx["ph"]), _h(client, fx["vn"], "VN")
    body = {"order_id": ctx["order_id"],
            "lines": [{"order_line_id": ctx["order_line_id"], "qty": 5, "reason_code": "OTHER"}]}
    first = client.post("/v1/returns", headers=ph, json=body).json()
    client.post(f"/v1/returns/{first['id']}/reject", headers=vn)
    again = client.post("/v1/returns", headers=ph, json=body)
    assert again.status_code == 201, "a rejected return must not keep holding the quantity"


def test_a_pharmacy_cannot_approve_its_own_return(client, db_engine):
    fx = _fixture(db_engine, "r5")
    ctx = _order_to_receipt_accepted(client, db_engine, fx, qty=4)
    ph = _h(client, fx["ph"])
    return_id = client.post("/v1/returns", headers=ph, json={
        "order_id": ctx["order_id"],
        "lines": [{"order_line_id": ctx["order_line_id"], "qty": 1, "reason_code": "DAMAGED"}],
    }).json()["id"]
    assert client.post(f"/v1/returns/{return_id}/approve", headers=ph).status_code == 403


# ------------------------------------------------------------------ credit notes

def _resolved_dispute(client, db_engine, fx, ctx) -> str:
    ph, vn = _h(client, fx["ph"]), _h(client, fx["vn"], "VN")
    dispute_id = client.post("/v1/disputes", headers=ph, json={
        "order_id": ctx["order_id"], "type": "RECEIPT_DISCREPANCY",
        "receipt_line_id": ctx["receipt_line_id"],
    }).json()["id"]
    client.post(f"/v1/disputes/{dispute_id}/assign", headers=vn)
    client.post(f"/v1/disputes/{dispute_id}/resolve", headers=vn, json={"outcome": "CREDIT_NOTE"})
    return dispute_id


def test_a_credit_note_must_cite_a_return_or_a_dispute(client, db_engine):
    """`ck_credit_note_origin` — money does not go back to a pharmacy unless
    it answers something. The 422 names the field; the CHECK is the backstop."""
    fx = _fixture(db_engine, "c0")
    ctx = _order_to_receipt_accepted(client, db_engine, fx, qty=4)
    _invoice(client, fx, ctx, "INV-c0")
    vn = _h(client, fx["vn"], "VN")
    r = client.post("/v1/credit-notes", headers=vn,
                    json={"order_id": ctx["order_id"], "amount": "10.00"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_credit_note_moves_the_ledger_and_lowers_exposure(client, db_engine):
    fx = _fixture(db_engine, "c1")
    ctx = _order_to_receipt_accepted(client, db_engine, fx, qty=10)
    inv = _invoice(client, fx, ctx, "INV-c1")
    dispute_id = _resolved_dispute(client, db_engine, fx, ctx)
    vn = _h(client, fx["vn"], "VN")

    before = client.get(f"/v1/credit-facilities/{fx['facility_id']}", headers=vn).json()
    cn = client.post("/v1/credit-notes", headers=vn,
                     json={"order_id": ctx["order_id"], "amount": "50.00", "dispute_id": dispute_id})
    assert cn.status_code == 201, cn.text
    assert cn.json()["ledger_posted"] is True

    after = client.get(f"/v1/credit-facilities/{fx['facility_id']}", headers=vn).json()
    assert Decimal(after["current_exposure"]) == Decimal(before["current_exposure"]) - Decimal("50.00")
    assert Decimal(inv["total_amount"]) > 0


def test_credit_notes_cannot_exceed_what_was_invoiced(client, db_engine):
    fx = _fixture(db_engine, "c2")
    ctx = _order_to_receipt_accepted(client, db_engine, fx, qty=2)
    inv = _invoice(client, fx, ctx, "INV-c2")
    dispute_id = _resolved_dispute(client, db_engine, fx, ctx)
    vn = _h(client, fx["vn"], "VN")
    too_much = Decimal(inv["total_amount"]) + Decimal("0.01")
    r = client.post("/v1/credit-notes", headers=vn,
                    json={"order_id": ctx["order_id"], "amount": str(too_much),
                          "dispute_id": dispute_id})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GUARD_FAILED"


def test_credit_note_amount_must_be_positive(client, db_engine):
    fx = _fixture(db_engine, "c3")
    ctx = _order_to_receipt_accepted(client, db_engine, fx, qty=2)
    _invoice(client, fx, ctx, "INV-c3")
    dispute_id = _resolved_dispute(client, db_engine, fx, ctx)
    vn = _h(client, fx["vn"], "VN")
    assert client.post("/v1/credit-notes", headers=vn,
                       json={"order_id": ctx["order_id"], "amount": "0",
                             "dispute_id": dispute_id}).status_code == 422


# ------------------------------------------------------------------ credit desk

def test_facility_exposure_is_derived_not_stored(client, db_engine):
    """The numbers on the credit screen must come from the ledger and the
    live order book every time — never from a column that can drift."""
    fx = _fixture(db_engine, "f1")
    vn = _h(client, fx["vn"], "VN")
    empty = client.get(f"/v1/credit-facilities/{fx['facility_id']}", headers=vn).json()
    assert empty["current_exposure"] == "0.00"
    assert empty["pending_exposure"] == "0.00"

    _order_to_receipt_accepted(client, db_engine, fx, qty=10)
    after = client.get(f"/v1/credit-facilities/{fx['facility_id']}", headers=vn).json()
    assert Decimal(after["pending_exposure"]) > 0, "a live order must show as pending exposure"
    assert Decimal(after["headroom"]) == (Decimal(after["limit_amount"])
                                          - Decimal(after["current_exposure"])
                                          - Decimal(after["pending_exposure"]))


def test_gate_check_blocks_an_order_larger_than_the_headroom(client, db_engine):
    fx = _fixture(db_engine, "f2", limit="100.00")
    vn = _h(client, fx["vn"], "VN")
    ok = client.post(f"/v1/credit-facilities/{fx['facility_id']}/gate-check", headers=vn,
                     json={"order_value": "50.00"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["blocked"] is False

    blocked = client.post(f"/v1/credit-facilities/{fx['facility_id']}/gate-check", headers=vn,
                          json={"order_value": "500.00"})
    assert blocked.json()["blocked"] is True
    assert "UPFRONT" in blocked.json()["options"]


def test_a_suspended_facility_blocks_everything(client, db_engine):
    fx = _fixture(db_engine, "f3")
    vn = _h(client, fx["vn"], "VN")
    assert client.post(f"/v1/credit-facilities/{fx['facility_id']}/suspend",
                       headers=vn).json()["status"] == "SUSPENDED"
    r = client.post(f"/v1/credit-facilities/{fx['facility_id']}/gate-check", headers=vn,
                    json={"order_value": "1.00"})
    assert r.json()["blocked"] is True

    assert client.post(f"/v1/credit-facilities/{fx['facility_id']}/suspend",
                       headers=vn).status_code == 409
    assert client.post(f"/v1/credit-facilities/{fx['facility_id']}/reinstate",
                       headers=vn).json()["status"] == "ACTIVE"


def test_limit_change_requires_a_reason_and_is_audited(client, db_engine):
    fx = _fixture(db_engine, "f4")
    vn = _h(client, fx["vn"], "VN")
    no_reason = client.patch(f"/v1/credit-facilities/{fx['facility_id']}", headers=vn,
                             json={"limit_amount": "5000.00"})
    assert no_reason.status_code == 422, "a limit change with no reason must not be accepted"

    ok = client.patch(f"/v1/credit-facilities/{fx['facility_id']}", headers=vn,
                      json={"limit_amount": "5000.00", "reason": "histórico de pagamento"})
    assert ok.status_code == 200, ok.text
    assert Decimal(ok.json()["limit_amount"]) == Decimal("5000.00")

    with db_engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM audit_event WHERE subject_id=:f "
                              "AND action_code='CREDIT_LIMIT_OVERRIDE'"), {"f": fx["facility_id"]}).scalar()
    assert n >= 1


def test_a_vendor_cannot_touch_another_vendors_facility(client, db_engine):
    fx_a = _fixture(db_engine, "f5")
    fx_b = _fixture(db_engine, "f6")
    vn_b = _h(client, fx_b["vn"], "VN")
    assert client.get(f"/v1/credit-facilities/{fx_a['facility_id']}", headers=vn_b).status_code == 404
    assert client.patch(f"/v1/credit-facilities/{fx_a['facility_id']}", headers=vn_b,
                        json={"limit_amount": "1.00", "reason": "x"}).status_code == 404


def test_ledger_is_append_only_and_corrections_are_new_signed_rows(client, db_engine):
    fx = _fixture(db_engine, "f7")
    ctx = _order_to_receipt_accepted(client, db_engine, fx, qty=10)
    _invoice(client, fx, ctx, "INV-f7")
    vn, pf = _h(client, fx["vn"], "VN"), _h(client, fx["pf"], "OP")

    before = client.get(f"/v1/credit-facilities/{fx['facility_id']}/ledger", headers=vn).json()
    assert before["items"], "the finalised invoice must have posted a ledger entry"

    adj = client.post(f"/v1/credit-facilities/{fx['facility_id']}/adjustments", headers=pf,
                      json={"amount": "-25.00", "reason": "erro de lançamento"})
    assert adj.status_code == 201, adj.text

    after = client.get(f"/v1/credit-facilities/{fx['facility_id']}/ledger", headers=vn).json()
    assert len(after["items"]) == len(before["items"]) + 1, \
        "a correction adds a row; it never edits the row that was wrong"
    assert Decimal(after["closing_balance"]) == Decimal(before["closing_balance"]) - Decimal("25.00")
    assert after["items"][-1]["entry_type"] == "ADJUSTMENT"


def test_the_database_itself_refuses_to_update_a_ledger_row(db_engine):
    """Not an application check — `rova_forbid_mutation()` on `ledger_entry`.
    If this ever passes, the append-only guarantee is gone."""
    with db_engine.begin() as conn:
        existing = conn.execute(text("SELECT id FROM ledger_entry LIMIT 1")).scalar()
    if existing is None:
        pytest.skip("no ledger entry to attempt a mutation against")
    with pytest.raises(Exception):
        with db_engine.begin() as conn:
            conn.execute(text("UPDATE ledger_entry SET amount = 0 WHERE id=:i"), {"i": existing})


def test_zero_adjustment_is_refused(client, db_engine):
    fx = _fixture(db_engine, "f8")
    pf = _h(client, fx["pf"], "OP")
    r = client.post(f"/v1/credit-facilities/{fx['facility_id']}/adjustments", headers=pf,
                    json={"amount": "0", "reason": "x"})
    assert r.status_code == 422


def test_statement_shows_the_open_invoice(client, db_engine):
    fx = _fixture(db_engine, "f9")
    ctx = _order_to_receipt_accepted(client, db_engine, fx, qty=10)
    inv = _invoice(client, fx, ctx, "INV-f9")
    vn = _h(client, fx["vn"], "VN")
    st = client.get(f"/v1/credit-facilities/{fx['facility_id']}/statement", headers=vn)
    assert st.status_code == 200, st.text
    assert st.json()["terms_days"] == 30
    mine = [i for i in st.json()["items"] if i["invoice_id"] == inv["id"]]
    assert mine, "a finalised unpaid invoice must appear on the statement"
    assert mine[0]["outstanding"] == inv["total_amount"]
    assert mine[0]["overdue"] is False


def test_creating_a_facility_twice_is_a_conflict(client, db_engine):
    fx = _fixture(db_engine, "f10")
    vn = _h(client, fx["vn"], "VN")
    r = client.post("/v1/credit-facilities", headers=vn, json={
        "vendor_id": fx["vendor_id"], "pharmacy_id": fx["pharmacy_id"], "limit_amount": "100.00",
    })
    assert r.status_code == 409


def test_a_non_zero_opening_balance_records_its_attestation(client, db_engine):
    fx = _fixture(db_engine, "f11")
    other = _fixture(db_engine, "f12")
    vn = _h(client, fx["vn"], "VN")
    r = client.post("/v1/credit-facilities", headers=vn, json={
        "vendor_id": fx["vendor_id"], "pharmacy_id": other["pharmacy_id"],
        "limit_amount": "9000.00", "opening_balance": "1200.00", "terms_days": 30,
    })
    assert r.status_code == 201, r.text
    assert r.json()["opening_balance_attested_at"] is not None, \
        "a claimed pre-existing debt must carry an attestation timestamp"
    assert r.json()["current_exposure"] == "1200.00"


def test_exposure_summary_orders_by_worst_headroom(client, db_engine):
    fx = _fixture(db_engine, "f13")
    vn = _h(client, fx["vn"], "VN")
    r = client.get("/v1/credit-exposure/summary", headers=vn)
    assert r.status_code == 200
    headrooms = [Decimal(i["headroom"]) for i in r.json()["items"]]
    assert headrooms == sorted(headrooms)
