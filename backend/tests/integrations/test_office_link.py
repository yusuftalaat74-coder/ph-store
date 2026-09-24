"""PH Office link (PH Office SPEC 5.9, AC-36..AC-40): outbox on checkout,
signed inbound events driving SM-03/04/05/11 as SYSTEM, the office_balance
mirror feeding the credit gate, the route lock, and /v1/me/*.

Everything is exercised with the flag both OFF (nothing changes) and ON."""
import json
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from rova.auth.security import hash_password

OUT_SECRET = "store-to-office-test-secret"
IN_SECRET = "office-to-store-test-secret"


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def office_on(monkeypatch):
    from rova.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "office_enabled", True)
    monkeypatch.setattr(s, "office_url", "http://phoffice.test")
    monkeypatch.setattr(s, "office_outbound_secret", OUT_SECRET)
    monkeypatch.setattr(s, "office_inbound_secret", IN_SECRET)
    yield s
    from rova.integrations.office import emitter
    emitter.set_http_client(None)


def _world(db_engine, *, limit="1000.00", auto_reroute=False):
    sx = uuid.uuid4().hex[:8]
    with db_engine.begin() as c:
        c.execute(text("INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
                       f"('org_v_{sx}', 'TV{sx}', 'Vendor {sx}', 'vendor {sx}', 'VENDOR'), "
                       f"('org_p_{sx}', 'TP{sx}', 'Farmacia {sx}', 'farmacia {sx}', 'PHARMACY')"))
        c.execute(text("INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
                       f"mov_amount, acceptance_mode, status) VALUES ('ven_{sx}', 'org_v_{sx}', 'MAPUTO_CIDADE', 'Vendor {sx}', "
                       "'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE')"))
        c.execute(text("INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
                       f"latitude, longitude, status, auto_reroute) VALUES ('pha_{sx}', 'org_p_{sx}', 'MAPUTO_CIDADE', 'A', "
                       f"'Farmacia {sx}', 'Av. X', 0, 0, 'ACTIVE', :ar)"), {"ar": auto_reroute})
        for tag in ("a", "b"):
            c.execute(text("INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
                           f"regulated_price, reviewer_ref, search_text) VALUES ('idx_{sx}_{tag}', 'Drug {tag}', 'comp', "
                           f"'500mg', '20', 'M', 'AUTHORISED', false, 'LOCAL:{sx}{tag}', 'd')"))
            c.execute(text("INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
                           f"pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES ('ofr_{sx}_{tag}', 'ven_{sx}', "
                           f"'idx_{sx}_{tag}', false, 100, '1', 300, 8.50, now())"))
        c.execute(text("INSERT INTO credit_facility (id, vendor_id, pharmacy_id, limit_amount, opening_balance, terms_days, "
                       f"status) VALUES ('crf_{sx}', 'ven_{sx}', 'pha_{sx}', :l, 0, 30, 'ACTIVE')"), {"l": limit})
        users = {}
        for key, org, roles in (("ph", f"org_p_{sx}", ["PharmacyBuyer", "PharmacyReceiver", "PharmacyAdmin"]),
                                ("desk", f"org_v_{sx}", ["VendorOrderDesk"]), ("vfin", f"org_v_{sx}", ["VendorFinance"])):
            phone = f"+2588{abs(hash(sx + key)) % 10**8:08d}"
            c.execute(text("INSERT INTO app_user (id, phone, name, password_hash) VALUES (:id, :p, :n, :h)"),
                      {"id": f"usr_{key}_{sx}", "p": phone, "n": key, "h": hash_password("rova-demo")})
            c.execute(text("INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES (:m, :u, :o, :r)"),
                      {"m": f"mem_{key}_{sx}", "u": f"usr_{key}_{sx}", "o": org, "r": roles})
            users[key] = phone
    return {"sx": sx, "vendor": f"ven_{sx}", "pharmacy": f"pha_{sx}", "facility": f"crf_{sx}",
            "a": f"idx_{sx}_a", "b": f"idx_{sx}_b", "users": users}


def _login(client, phone):
    r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": "PH"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _checkout(client, w, qty_a=10, qty_b=2):
    h = _login(client, w["users"]["ph"])
    rid = client.post("/v1/requests", json={"mode": "CATALOGUE", "allocation_strategy": "FEWEST_VENDORS"}, headers=h).json()["id"]
    for prod, qty in ((w["a"], qty_a), (w["b"], qty_b)):
        if qty:
            assert client.post(f"/v1/requests/{rid}/lines", json={"index_product_id": prod, "qty_requested": qty},
                               headers=h).status_code == 200
    r = client.post(f"/v1/requests/{rid}/checkout", headers={**h, "Idempotency-Key": uuid.uuid4().hex})
    return r


def _sign(body: bytes, secret=IN_SECRET, ts=None):
    from rova.integrations.office.signing import sign
    ts = ts or int(time.time())
    return {"X-PH-Timestamp": str(ts), "X-PH-Signature": sign(secret, ts, body), "Content-Type": "application/json"}


_V = {}


def _event(client, event_type, aggregate_type, aggregate_id, payload, *, version=None, secret=IN_SECRET, event_id=None):
    if version is None:
        _V[aggregate_id] = _V.get(aggregate_id, 0) + 1
        version = _V[aggregate_id]
    env = {"event_id": event_id or f"evt_bo_{uuid.uuid4().hex}", "event_type": event_type, "source": "phoffice",
           "seq": 1, "occurred_at": "2026-09-24T09:40:11Z", "aggregate_type": aggregate_type,
           "aggregate_id": aggregate_id, "aggregate_version": version, "payload": payload}
    body = json.dumps(env).encode()
    return client.post("/v1/office/events", content=body, headers=_sign(body, secret)), env


def _q(db_engine, sql, **p):
    with db_engine.connect() as c:
        return c.execute(text(sql), p).mappings().all()


def _order_lines(db_engine, order_id):
    return _q(db_engine, "SELECT * FROM order_line WHERE order_id=:o ORDER BY index_product_id", o=order_id)


# ------------------------------------------------------------------ flag OFF: nothing changes

def test_flag_off_checkout_writes_no_outbox_and_receiver_is_closed(client, db_engine):
    w = _world(db_engine)
    r = _checkout(client, w)
    assert r.status_code == 200, r.text
    oid = r.json()["orders"][0]["id"]
    assert _q(db_engine, "SELECT COUNT(*) AS n FROM integration_outbox WHERE aggregate_id=:o", o=oid)[0]["n"] == 0
    body = b"{}"
    assert client.post("/v1/office/events", content=body, headers=_sign(body)).status_code == 503
    # AC-39 (false half): with the flag off the vendor desk still accepts in PH Store
    lines = _order_lines(db_engine, oid)
    r = client.post(f"/v1/orders/{oid}/accept", headers=_login(client, w["users"]["desk"]),
                    json={"lines": [{"order_line_id": l["id"], "confirmed_qty": l["ordered_qty"]} for l in lines]})
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED"
    from rova.jobs.tick import JOBS
    assert JOBS["office_outbox"]() == 0


def test_current_exposure_ignores_office_until_a_balance_arrives(db_engine, session, monkeypatch):
    """SPEC 5.9.4 + 1.4: office_balance counts only while the link is enabled,
    so rolling back the cutover is the flag alone."""
    from rova.config import get_settings
    from rova.credit.exposure import current_exposure
    w = _world(db_engine)
    assert current_exposure(session, w["facility"]) == Decimal("0")
    session.execute(text("UPDATE credit_facility SET office_balance=123.45 WHERE id=:f"), {"f": w["facility"]})
    assert current_exposure(session, w["facility"]) == Decimal("0")          # flag off: mirror ignored
    monkeypatch.setattr(get_settings(), "office_enabled", True)
    assert current_exposure(session, w["facility"]) == Decimal("123.45")     # flag on: mirror is the exposure
    monkeypatch.setattr(get_settings(), "office_enabled", False)
    assert current_exposure(session, w["facility"]) == Decimal("0")          # rollback = flag only


# ------------------------------------------------------------------ flag ON

def test_ac36_checkout_writes_order_created(client, db_engine, office_on):
    w = _world(db_engine)
    r = _checkout(client, w)
    assert r.status_code == 200, r.text
    oid = r.json()["orders"][0]["id"]
    rows = _q(db_engine, "SELECT * FROM integration_outbox WHERE aggregate_id=:o", o=oid)
    assert len(rows) == 1 and rows[0]["event_type"] == "order.created" and rows[0]["aggregate_version"] == 0
    p = rows[0]["payload"]
    assert p["order"]["id"] == oid and p["order"]["vendor_id"] == w["vendor"] and p["order"]["payment_terms"] == "CREDIT_N_DAYS"
    assert p["pharmacy"]["id"] == w["pharmacy"] and p["pharmacy"]["credit_facility_id"] == w["facility"]
    assert p["pharmacy"]["credit_facility"]["limit_amount"] == "1000.00"
    assert sorted((l["index_product_id"], l["ordered_qty"], l["unit_price"]) for l in p["lines"]) == [
        (w["a"], 10, "8.50"), (w["b"], 2, "8.50")]


def test_ac37_order_accepted_moves_sm03_sm04_as_system(client, db_engine, office_on):
    w = _world(db_engine)
    oid = _checkout(client, w).json()["orders"][0]["id"]
    la, lb = _order_lines(db_engine, oid)
    payload = {"store_order_id": oid, "promised_dispatch_at": "2026-09-25T14:00:00Z",
               "lines": [{"store_order_line_id": la["id"], "confirmed_qty": 8, "short_reason": "PARTIAL_ACCEPTANCE"},
                         {"store_order_line_id": lb["id"], "confirmed_qty": 2}],
               "actor": {"office_user_id": "bo_usr_1", "role": "OpsAgent"}}
    # bad signature first: 401, nothing recorded
    r, env = _event(client, "order.accepted", "order", oid, payload, secret="wrong")
    assert r.status_code == 401 and r.json()["error"]["code"] == "SIGNATURE_INVALID"
    assert _q(db_engine, "SELECT COUNT(*) AS n FROM integration_inbound_event WHERE event_id=:e", e=env["event_id"])[0]["n"] == 0
    r, env = _event(client, "order.accepted", "order", oid, payload)
    assert r.status_code == 200 and r.json()["processing"] == "PROCESSED", r.text
    order = _q(db_engine, 'SELECT * FROM "order" WHERE id=:o', o=oid)[0]
    assert order["status"] == "ACCEPTED" and order["promised_dispatch_at"] is not None
    la, lb = _order_lines(db_engine, oid)
    assert (la["fulfilment_status"], la["confirmed_qty"]) == ("LINE_SHORT", 8)
    assert (lb["fulfilment_status"], lb["confirmed_qty"]) == ("LINE_FULL", 2)
    st = _q(db_engine, "SELECT * FROM state_transition WHERE subject_id=:o AND machine='SM-03'", o=oid)
    assert st[-1]["trigger"] == "ACCEPT" and st[-1]["actor_role"] == "SYSTEM" and st[-1]["actor_user_id"] is None
    assert st[-1]["notes"]["via"] == "phoffice" and st[-1]["notes"]["event_id"] == env["event_id"]
    line_st = _q(db_engine, "SELECT actor_role, trigger FROM state_transition WHERE subject_id=:l", l=la["id"])
    assert line_st == [{"actor_role": "SYSTEM", "trigger": "CONFIRM_SHORT"}]
    # same event again: duplicate; an older version: SKIPPED
    body = json.dumps(env).encode()
    assert client.post("/v1/office/events", content=body, headers=_sign(body)).json()["status"] == "duplicate"
    r, _ = _event(client, "order.rejected", "order", oid, {"store_order_id": oid}, version=env["aggregate_version"])
    assert r.json()["processing"] == "SKIPPED"
    # a stale timestamp is refused
    body = json.dumps({**env, "event_id": "evt_new"}).encode()
    assert client.post("/v1/office/events", content=body, headers=_sign(body, ts=int(time.time()) - 400)).status_code == 401


def test_office_drives_the_whole_order_and_money(client, db_engine, office_on):
    w = _world(db_engine)
    oid = _checkout(client, w, qty_a=4, qty_b=2).json()["orders"][0]["id"]
    la, lb = _order_lines(db_engine, oid)
    assert _event(client, "order.accepted", "order", oid, {"store_order_id": oid, "lines": [
        {"store_order_line_id": la["id"], "confirmed_qty": 4}, {"store_order_line_id": lb["id"], "confirmed_qty": 2}]}
    )[0].json()["processing"] == "PROCESSED"
    picked = {"store_order_id": oid, "lines": [
        {"store_order_line_id": l["id"], "picked_qty": l["confirmed_qty"], "lot_number": f"LOT-{i}", "batch_number": f"B-{i}",
         "expiry_date": "2027-12-31", "seal_ids": [f"SEAL-{w['sx']}-{i}"]} for i, l in enumerate(_order_lines(db_engine, oid))]}
    assert _event(client, "order.picked", "order", oid, picked)[0].json()["processing"] == "PROCESSED"
    r, _ = _event(client, "order.dispatched", "order", oid, {"store_order_id": oid})
    assert r.json()["processing"] == "PROCESSED", r.text
    assert _q(db_engine, "SELECT COUNT(*) AS n FROM traceability_event te JOIN order_line ol ON ol.id=te.order_line_id "
                         "WHERE ol.order_id=:o AND te.event_type='DISPATCHED'", o=oid)[0]["n"] == 2
    r, _ = _event(client, "delivery.attempted", "order", oid, {"store_order_id": oid, "outcome": "DELIVERED",
                                                              "proof_code": "SIGN"})
    assert r.json()["processing"] == "PROCESSED", r.text
    assert _q(db_engine, 'SELECT status FROM "order" WHERE id=:o', o=oid)[0]["status"] == "DELIVERED_PENDING_RECEIPT"
    # the pharmacy confirms receipt in the app, as today -> order.receipt_accepted goes to the outbox
    lines = _order_lines(db_engine, oid)
    r = client.post(f"/v1/orders/{oid}/receipt", headers=_login(client, w["users"]["ph"]), json={"lines": [
        {"order_line_id": l["id"], "accepted_qty": l["confirmed_qty"], "rejected_qty": 0} for l in lines]})
    assert r.status_code == 200 and r.json()["status"] == "RECEIPT_ACCEPTED"
    out = _q(db_engine, "SELECT event_type, payload FROM integration_outbox WHERE aggregate_id=:o ORDER BY seq", o=oid)
    assert [o["event_type"] for o in out] == ["order.created", "order.receipt_accepted"]   # no echo of Office's own events
    assert {l["store_order_line_id"] for l in out[1]["payload"]["lines"]} == {la["id"], lb["id"]}
    # invoice + payment mirrors
    inv = {"store_order_id": oid, "office_invoice_id": f"bo_inv_{w['sx']}", "number": f"FT T/2026/{w['sx']}",
           "issue_date": "2026-09-25", "due_date": "2026-10-25", "net_total": "51.00", "tax_total": "0.00",
           "gross_total": "51.00", "lines": [{"store_order_line_id": l["id"], "qty": l["confirmed_qty"], "unit_price": "8.50",
                                               "tax_amount": "0.00", "line_total": "0"} for l in lines]}
    assert _event(client, "invoice.issued", "order", oid, inv)[0].json()["processing"] == "PROCESSED"
    invoice = _q(db_engine, "SELECT * FROM invoice WHERE office_invoice_id=:i", i=inv["office_invoice_id"])[0]
    assert invoice["status"] == "AWAITING_PAYMENT" and invoice["source"] == "OFFICE" and invoice["price_match_flag"] == "OK"
    assert _q(db_engine, "SELECT amount FROM ledger_entry WHERE reference_id=:i", i=invoice["id"])[0]["amount"] == Decimal("51.00")
    pay = {"office_payment_id": f"bo_pay_{w['sx']}", "store_pharmacy_id": w["pharmacy"], "store_vendor_id": w["vendor"],
           "method": "M_PESA", "external_ref": "MP1", "amount": "51.00", "received_at": "2026-09-26T10:02:00Z",
           "allocations": [{"office_invoice_id": inv["office_invoice_id"], "store_order_id": oid, "amount": "51.00"}]}
    cid = f"bo_cus_{w['sx']}"
    assert _event(client, "payment.received", "customer_account", cid, pay)[0].json()["processing"] == "PROCESSED"
    assert _q(db_engine, "SELECT status FROM invoice WHERE id=:i", i=invoice["id"])[0]["status"] == "PAID"
    p = _q(db_engine, "SELECT * FROM payment WHERE office_payment_id=:p", p=pay["office_payment_id"])[0]
    assert p["method"] == "MOBILE_MONEY" and p["recorded_by_user_id"] is None


def test_ac38_account_updated_feeds_the_credit_gate(client, db_engine, office_on):
    w = _world(db_engine, limit="1000.00")
    snap = {"store_pharmacy_id": w["pharmacy"], "store_vendor_id": w["vendor"], "store_credit_facility_id": w["facility"],
            "as_of": "2026-09-24T10:02:00Z", "balance": "999.99", "credit_limit": "1000.00", "terms_days": 30,
            "status": "ACTIVE", "hold_reason": None, "overdue_amount": "0.00", "oldest_due_date": None,
            "open_documents": [{"type": "INVOICE", "number": "FT X/2026/000001", "date": "2026-09-15",
                                "due_date": "2026-10-15", "amount": "999.99", "open_amount": "999.99", "order_number": "RQ"}],
            "ageing": {"current": "999.99", "d31_60": "0.00", "d61_90": "0.00", "d90_plus": "0.00"}}
    r, _ = _event(client, "account.updated", "customer_account", f"bo_cus_{w['sx']}", snap)
    assert r.json()["processing"] == "PROCESSED"
    f = _q(db_engine, "SELECT * FROM credit_facility WHERE id=:f", f=w["facility"])[0]
    assert f["office_balance"] == Decimal("999.99") and f["office_credit_limit"] == Decimal("1000.00")
    assert f["office_hold"] is False and f["office_synced_at"] is not None
    ph = _q(db_engine, "SELECT office_account_snapshot FROM pharmacy_account WHERE id=:p", p=w["pharmacy"])[0]
    assert ph["office_account_snapshot"]["vendors"][w["vendor"]]["balance"] == "999.99"
    # limit - 0.01 already used: an order of 0.02 is blocked by the gate
    from rova.credit.gate import check
    from sqlalchemy.orm import sessionmaker
    s = sessionmaker(bind=db_engine)()
    try:
        assert check(s, w["facility"], w["vendor"], w["pharmacy"], Decimal("0.02")).blocked is True
        assert check(s, w["facility"], w["vendor"], w["pharmacy"], Decimal("0.01")).blocked is False
    finally:
        s.close()
    # and checkout of 8.50 is refused on credit terms
    r = _checkout(client, w, qty_a=1, qty_b=0)
    assert r.status_code == 409 and r.json()["error"]["rule"] == "R-044"


def test_ac39_vendor_routes_locked_when_enabled(client, db_engine, office_on):
    w = _world(db_engine)
    oid = _checkout(client, w).json()["orders"][0]["id"]
    lines = _order_lines(db_engine, oid)
    desk = _login(client, w["users"]["desk"])
    r = client.post(f"/v1/orders/{oid}/accept", headers=desk,
                    json={"lines": [{"order_line_id": l["id"], "confirmed_qty": l["ordered_qty"]} for l in lines]})
    assert r.status_code == 409 and r.json()["error"] == {"code": "GUARD_FAILED", "message": "managed by PH Office",
                                                          "details": []}
    vfin = _login(client, w["users"]["vfin"])
    assert client.post("/v1/payments", headers=vfin, json={
        "pharmacy_id": w["pharmacy"], "vendor_id": w["vendor"], "method": "CASH_ON_DELIVERY", "amount": "1.00",
        "allocations": []}).status_code == 409
    assert client.post(f"/v1/orders/{oid}/reject", headers=desk).status_code == 409
    assert _q(db_engine, 'SELECT status FROM "order" WHERE id=:o', o=oid)[0]["status"] == "PENDING_ACCEPTANCE"


class _FakeOffice:
    def __init__(self, status=200, body=None, raise_exc=False):
        self.status, self.body, self.raise_exc, self.calls = status, body or {}, raise_exc, []

    def get(self, url, params=None, headers=None):
        from rova.integrations.office.signing import verify
        self.calls.append((url, params, verify(OUT_SECRET, {k.lower(): v for k, v in headers.items()}, b"")))
        if self.raise_exc:
            raise ConnectionError("down")
        r = type("R", (), {})()
        r.status_code, r.json, r.text = self.status, (lambda: self.body), json.dumps(self.body)
        return r


def test_ac40_me_account_and_statement(client, db_engine, office_on):
    from rova.integrations.office import emitter
    w = _world(db_engine)
    snap = {"store_pharmacy_id": w["pharmacy"], "store_vendor_id": w["vendor"], "as_of": "2026-09-24T10:02:00+00:00",
            "balance": "120.00", "credit_limit": "1000.00", "terms_days": 30, "status": "ACTIVE", "hold_reason": None,
            "overdue_amount": "20.00", "oldest_due_date": "2026-09-01",
            "open_documents": [{"type": "INVOICE", "number": "FT A/2026/000009", "date": "2026-08-01",
                                "due_date": "2026-09-01", "amount": "120.00", "open_amount": "120.00", "order_number": "RQ"}],
            "ageing": {"current": "100.00", "d31_60": "20.00", "d61_90": "0.00", "d90_plus": "0.00"}}
    _event(client, "account.updated", "customer_account", f"bo_cus_{w['sx']}", snap)
    h = _login(client, w["users"]["ph"])
    acc = client.get("/v1/me/account", headers=h).json()
    assert acc["balance"] == "120.00" and acc["credit_limit"] == "1000.00" and acc["available"] == "880.00"
    assert acc["overdue_amount"] == "20.00" and acc["as_of"] == "2026-09-24T10:02:00+00:00"
    assert acc["open_documents"][0]["number"] == "FT A/2026/000009"
    fake = _FakeOffice(body={"lines": [], "closing_balance": "120.00"})
    emitter.set_http_client(fake)
    r = client.get("/v1/me/statement", headers=h, params={"from": "2026-01-01"})
    assert r.status_code == 200 and r.json()["closing_balance"] == "120.00"
    url, params, signed_ok = fake.calls[0]
    assert url.endswith(f"/office/v1/integration/pharmacies/{w['pharmacy']}/statement") and signed_ok
    assert params == {"vendor_id": w["vendor"], "from": "2026-01-01"}
    emitter.set_http_client(_FakeOffice(raise_exc=True))
    r = client.get("/v1/me/statement", headers=h)
    assert r.status_code == 503 and r.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert client.get("/v1/me/account", headers=_login(client, w["users"]["desk"])).status_code == 403


class _FakeReceiver:
    def __init__(self):
        self.down, self.got = set(), []

    def post(self, url, content, headers):
        from rova.integrations.office.signing import verify
        assert verify(OUT_SECRET, {k.lower(): v for k, v in headers.items()}, content)
        env = json.loads(content)
        if env["aggregate_id"] in self.down:
            raise ConnectionError("office down")
        self.got.append((env["aggregate_id"], env["event_type"]))
        r = type("R", (), {"status_code": 200, "text": "{}"})()
        return r


def test_emitter_orders_per_aggregate_and_backs_off(client, db_engine, office_on):
    from rova.core.clock import freeze, now, unfreeze
    from rova.integrations.office import emitter
    w = _world(db_engine)
    o1 = _checkout(client, w).json()["orders"][0]["id"]
    o2 = _checkout(client, w).json()["orders"][0]["id"]
    with db_engine.begin() as c:     # a second event queued behind o1's order.created
        c.execute(text("INSERT INTO integration_outbox (id, event_type, aggregate_type, aggregate_id, aggregate_version, "
                       "payload) VALUES (:id, 'order.cancelled', 'order', :o, 1, '{}')"), {"id": f"obx_t_{w['sx']}", "o": o1})
    fake = _FakeReceiver()
    fake.down.add(o1)
    emitter.set_http_client(fake)
    from rova.jobs.tick import JOBS
    JOBS["office_outbox"]()
    st = {r["event_type"]: r for r in _q(db_engine, "SELECT * FROM integration_outbox WHERE aggregate_id=:o", o=o1)}
    assert st["order.created"]["status"] == "FAILED" and st["order.created"]["attempts"] == 1
    assert st["order.cancelled"]["attempts"] == 0                        # held behind the failed one
    assert (o2, "order.created") in fake.got                             # another aggregate went through
    fake.down.clear()
    freeze(now() + __import__("datetime").timedelta(seconds=31))
    try:
        JOBS["office_outbox"]()
    finally:
        unfreeze()
    assert [e for a, e in fake.got if a == o1] == ["order.created", "order.cancelled"]


def test_hooks_emit_pharmacy_and_fee_events(client, db_engine, office_on, session):
    from rova.domain.hooks import wire
    from rova.domain.machines.registry import MACHINES
    wire()
    w = _world(db_engine)
    from rova.integrations.office.inbound import office_actor
    MACHINES["SM-07"].apply(session, w["pharmacy"], "MANUAL_SUSPEND", office_actor("PlatformAdmin"))
    session.commit()
    ev = _q(db_engine, "SELECT event_type, payload FROM integration_outbox WHERE aggregate_id=:p", p=w["pharmacy"])
    assert ev[0]["event_type"] == "pharmacy.suspended" and ev[0]["payload"]["suspension_cause"] == "MANUAL_INCIDENT"
    from rova.fees.accrual import _insert_fee_event
    fs = f"fsc_{w['sx']}"
    session.execute(text(
        "INSERT INTO fee_schedule (id, type, payer, rate_or_amount, scope_type, scope_id, effective_from, earning_event, "
        "legal_status, created_by_user_id) VALUES (:id, 'PHARMACY_SERVICE_FEE', 'PHARMACY', 1.50, 'PHARMACY', :p, "
        "'2026-01-01', 'RECEIPT_ACCEPTED', 'STANDARD', :u)"), {"id": fs, "p": w["pharmacy"], "u": f"usr_ph_{w['sx']}"})
    session.commit()
    oid = _checkout(client, w).json()["orders"][0]["id"]
    _insert_fee_event(session, fee_schedule_id=fs, payer_org_id=f"org_p_{w['sx']}", order_id=oid,
                      base_amount=Decimal("1.50"), amount=Decimal("1.50"), attribution="INCREMENTAL")
    session.commit()
    fee = _q(db_engine, "SELECT aggregate_version, payload FROM integration_outbox WHERE event_type='fee.accrued' "
                        "AND payload->>'order_id'=:o", o=oid)
    assert len(fee) == 1 and fee[0]["aggregate_version"] == 0
    p = fee[0]["payload"]
    assert p["payer_type"] == "PHARMACY" and p["payer_pharmacy_id"] == w["pharmacy"] and p["amount"] == "1.50"
    assert p["fee_type"] == "PHARMACY_SERVICE_FEE" and p["vendor_id"] == w["vendor"]


def test_pull_endpoints_are_hmac_protected(client, db_engine, office_on):
    w = _world(db_engine)
    oid = _checkout(client, w).json()["orders"][0]["id"]
    assert client.get(f"/v1/office/orders/{oid}").status_code == 401
    r = client.get(f"/v1/office/orders/{oid}", headers=_sign(b""))
    assert r.status_code == 200 and r.json()["id"] == oid and len(r.json()["lines"]) == 2
    r = client.get("/v1/office/reconcile/orders", headers=_sign(b""), params={"from": "2020-01-01"})
    assert oid in [i["order_id"] for i in r.json()["items"]]
    r = client.get("/v1/office/reconcile/facilities", headers=_sign(b""))
    assert w["facility"] in [i["facility_id"] for i in r.json()["items"]]


def test_returns_stay_in_store_when_linked_and_emit_received(client, db_engine, office_on, session):
    """SPEC 1.3 (v1.1): approve / reject / receive of a return are NOT locked by
    the link (BO-SM-10 is deferred, the approval is a Store decision); Store
    emits return.approved and return.received so PH Office can book the credit
    note. /credit-notes stays locked (PH Office owns credit notes)."""
    from rova.domain.hooks import wire
    from rova.domain.machines.registry import MACHINES
    wire()
    w = _world(db_engine)
    oid = _checkout(client, w).json()["orders"][0]["id"]
    line = _order_lines(db_engine, oid)[0]
    sx = w["sx"]
    session.execute(text('INSERT INTO "return" (id, order_id, rma_number, origin, status) '
                         "VALUES (:id, :o, :rma, 'PHARMACY_RMA', 'REQUESTED')"),
                    {"id": f"rtn_{sx}", "o": oid, "rma": f"RMA-{sx}"})
    session.execute(text("INSERT INTO return_line (id, return_id, order_line_id, qty, reason_code) "
                         "VALUES (:id, :r, :l, 1, 'DAMAGED')"), {"id": f"rtl_{sx}", "r": f"rtn_{sx}", "l": line["id"]})
    session.commit()
    vfin = _login(client, w["users"]["vfin"])
    r = client.post(f"/v1/returns/rtn_{sx}/approve", headers=vfin)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"
    for trig, actor in (("SHIP", "Dispatcher"),):
        from rova.integrations.office.inbound import office_actor
        MACHINES["SM-10"].apply(session, f"rtn_{sx}", trig, office_actor(actor))
    session.commit()
    r = client.post(f"/v1/returns/rtn_{sx}/receive", headers=vfin)
    assert r.status_code == 200 and r.json()["status"] == "RECEIVED_BY_VENDOR", r.text
    ev = [e["event_type"] for e in _q(db_engine, "SELECT event_type FROM integration_outbox WHERE aggregate_id=:r ORDER BY seq",
                                     r=f"rtn_{sx}")]
    assert ev == ["return.approved", "return.received"]
    assert client.post("/v1/credit-notes", headers=vfin, json={"order_id": oid, "amount": "1.00",
                       "return_id": f"rtn_{sx}"}).status_code == 409


# ------------------------------------------------------------------ REVIEW F-1: credit_note.issued / payment.reversed

def _office_order_to_invoice(client, db_engine, w, qty_a=4, qty_b=2):
    """Drive one order Office-side up to an invoice mirror (AWAITING_PAYMENT)."""
    oid = _checkout(client, w, qty_a=qty_a, qty_b=qty_b).json()["orders"][0]["id"]
    la, lb = _order_lines(db_engine, oid)
    assert _event(client, "order.accepted", "order", oid, {"store_order_id": oid, "lines": [
        {"store_order_line_id": la["id"], "confirmed_qty": qty_a},
        {"store_order_line_id": lb["id"], "confirmed_qty": qty_b}]})[0].json()["processing"] == "PROCESSED"
    picked = {"store_order_id": oid, "lines": [
        {"store_order_line_id": l["id"], "picked_qty": l["confirmed_qty"], "lot_number": f"LOT-{i}",
         "expiry_date": "2027-12-31", "seal_ids": [f"SEAL-{oid}-{i}"]} for i, l in enumerate(_order_lines(db_engine, oid))]}
    assert _event(client, "order.picked", "order", oid, picked)[0].json()["processing"] == "PROCESSED"
    assert _event(client, "order.dispatched", "order", oid, {"store_order_id": oid})[0].json()["processing"] == "PROCESSED"
    assert _event(client, "delivery.attempted", "order", oid, {"store_order_id": oid, "outcome": "DELIVERED"}
                  )[0].json()["processing"] == "PROCESSED"
    lines = _order_lines(db_engine, oid)
    r = client.post(f"/v1/orders/{oid}/receipt", headers=_login(client, w["users"]["ph"]), json={"lines": [
        {"order_line_id": l["id"], "accepted_qty": l["confirmed_qty"], "rejected_qty": 0} for l in lines]})
    assert r.status_code == 200, r.text
    total = sum(l["confirmed_qty"] for l in lines) * Decimal("8.50")
    inv = {"store_order_id": oid, "office_invoice_id": f"bo_inv_{oid}", "number": f"FT T/2026/{oid}",
           "issue_date": "2026-09-25", "due_date": "2026-10-25", "net_total": str(total), "tax_total": "0.00",
           "gross_total": str(total), "lines": [{"store_order_line_id": l["id"], "qty": l["confirmed_qty"],
                                                  "unit_price": "8.50", "tax_amount": "0.00", "line_total": "0"}
                                                 for l in lines]}
    assert _event(client, "invoice.issued", "order", oid, inv)[0].json()["processing"] == "PROCESSED"
    invoice = _q(db_engine, "SELECT * FROM invoice WHERE office_invoice_id=:i", i=inv["office_invoice_id"])[0]
    return oid, invoice


def _ledger(db_engine, facility):
    return _q(db_engine, "SELECT COALESCE(SUM(amount),0) AS s FROM ledger_entry WHERE credit_facility_id=:f", f=facility)[0]["s"]


def test_ac46_credit_note_issued_mirrors_and_closes_the_return(client, db_engine, office_on, session):
    """SPEC v1.1 AC-46 / REVIEW F-1: credit_note.issued with return_ref for a
    return in RECEIVED_BY_VENDOR -> credit_note mirror + ledger_entry(CREDIT_NOTE)
    + the return CLOSED (as SYSTEM, via=phoffice). A note citing no Store return
    (GOODWILL) is SKIPPED with a clear reason, never FAILED."""
    from rova.domain.hooks import wire
    from rova.domain.machines.registry import MACHINES
    from rova.integrations.office.inbound import office_actor
    wire()
    w = _world(db_engine)
    oid, invoice = _office_order_to_invoice(client, db_engine, w)
    assert _ledger(db_engine, w["facility"]) == Decimal("51.00")
    sx, line = w["sx"], _order_lines(db_engine, oid)[0]
    session.execute(text('INSERT INTO "return" (id, order_id, rma_number, origin, status) '
                         "VALUES (:id, :o, :rma, 'PHARMACY_RMA', 'REQUESTED')"),
                    {"id": f"rtn_{sx}", "o": oid, "rma": f"RMA-{sx}"})
    session.execute(text("INSERT INTO return_line (id, return_id, order_line_id, qty, reason_code) "
                         "VALUES (:id, :r, :l, 1, 'DAMAGED')"), {"id": f"rtl_{sx}", "r": f"rtn_{sx}", "l": line["id"]})
    session.commit()
    vfin = _login(client, w["users"]["vfin"])
    assert client.post(f"/v1/returns/rtn_{sx}/approve", headers=vfin).status_code == 200
    MACHINES["SM-10"].apply(session, f"rtn_{sx}", "SHIP", office_actor("Dispatcher"))
    session.commit()
    assert client.post(f"/v1/returns/rtn_{sx}/receive", headers=vfin).json()["status"] == "RECEIVED_BY_VENDOR"

    cn = {"store_order_id": oid, "office_credit_note_id": f"bo_crn_{sx}", "number": f"NC T/2026/{sx}",
          "office_invoice_id": invoice["office_invoice_id"], "reason": "RETURN", "gross_total": "8.50",
          "return_ref": f"rtn_{sx}"}
    r, _ = _event(client, "credit_note.issued", "order", oid, cn)
    assert r.status_code == 200 and r.json()["processing"] == "PROCESSED", r.text
    row = _q(db_engine, "SELECT * FROM credit_note WHERE office_credit_note_id=:c", c=cn["office_credit_note_id"])
    assert len(row) == 1
    row = row[0]
    assert (row["order_id"], row["return_id"], row["dispute_id"], row["amount"]) == (oid, f"rtn_{sx}", None, Decimal("8.50"))
    assert (row["vendor_id"], row["pharmacy_id"]) == (w["vendor"], w["pharmacy"])
    led = _q(db_engine, "SELECT entry_type, amount FROM ledger_entry WHERE reference_id=:c", c=row["id"])
    assert led == [{"entry_type": "CREDIT_NOTE", "amount": Decimal("-8.50")}]
    assert _ledger(db_engine, w["facility"]) == Decimal("42.50")
    assert _q(db_engine, 'SELECT status FROM "return" WHERE id=:r', r=f"rtn_{sx}")[0]["status"] == "CLOSED"
    st = _q(db_engine, "SELECT * FROM state_transition WHERE subject_id=:r AND trigger='CLOSE_WITH_CREDIT_NOTE'",
            r=f"rtn_{sx}")[0]
    assert st["actor_role"] == "SYSTEM" and st["actor_user_id"] is None and st["notes"]["via"] == "phoffice"
    # the same credit note again (new event id, later version): nothing doubles
    r, _ = _event(client, "credit_note.issued", "order", oid, cn)
    assert r.json()["processing"] == "SKIPPED"
    assert _ledger(db_engine, w["facility"]) == Decimal("42.50")
    # a GOODWILL note with no Store return: SKIPPED with a reason, no row, no ledger movement
    gw = {**cn, "office_credit_note_id": f"bo_crn_gw_{sx}", "reason": "GOODWILL", "return_ref": None}
    r, env = _event(client, "credit_note.issued", "order", oid, gw)
    assert r.status_code == 200 and r.json()["processing"] == "SKIPPED"
    ev = _q(db_engine, "SELECT status, error FROM integration_inbound_event WHERE event_id=:e", e=env["event_id"])[0]
    assert ev["status"] == "SKIPPED" and "cites no Store return" in ev["error"] and "account.updated" in ev["error"]
    assert _q(db_engine, "SELECT COUNT(*) AS n FROM credit_note WHERE order_id=:o", o=oid)[0]["n"] == 1
    assert _ledger(db_engine, w["facility"]) == Decimal("42.50")
    # a return_ref naming a return of ANOTHER order is not trusted either
    r, _ = _event(client, "credit_note.issued", "order", oid,
                  {**cn, "office_credit_note_id": f"bo_crn_x_{sx}", "return_ref": "rtn_does_not_exist"})
    assert r.json()["processing"] == "SKIPPED"


def test_payment_reversed_restores_the_ledger_and_reopens_the_invoice(client, db_engine, office_on):
    """SPEC 5.6 payment.reversed / REVIEW F-1: ledger_entry(ADJUSTMENT, +amount)
    against the facility and SM-11 moved back for the invoices it had paid."""
    w = _world(db_engine)
    oid, invoice = _office_order_to_invoice(client, db_engine, w)
    cid = f"bo_cus_{w['sx']}"
    pays = [{"office_payment_id": f"bo_pay_{w['sx']}_{n}", "store_pharmacy_id": w["pharmacy"],
             "store_vendor_id": w["vendor"], "method": "CASH", "amount": amt, "received_at": "2026-09-26T10:02:00Z",
             "allocations": [{"office_invoice_id": invoice["office_invoice_id"], "store_order_id": oid, "amount": amt}]}
            for n, amt in ((1, "20.00"), (2, "31.00"))]
    for p in pays:
        assert _event(client, "payment.received", "customer_account", cid, p)[0].json()["processing"] == "PROCESSED"
    assert _q(db_engine, "SELECT status FROM invoice WHERE id=:i", i=invoice["id"])[0]["status"] == "PAID"
    assert _ledger(db_engine, w["facility"]) == Decimal("0.00")
    # reverse the second payment: PAID -> PARTIALLY_PAID, ledger back to 31.00
    rev = {"office_payment_id": pays[1]["office_payment_id"], "amount": "31.00", "reason": "cheque bounced"}
    r, _ = _event(client, "payment.reversed", "customer_account", cid, rev)
    assert r.status_code == 200 and r.json()["processing"] == "PROCESSED", r.text
    pid2 = _q(db_engine, "SELECT id FROM payment WHERE office_payment_id=:p", p=rev["office_payment_id"])[0]["id"]
    assert _q(db_engine, "SELECT entry_type, reference_type, amount FROM ledger_entry WHERE reference_id=:p "
                         "ORDER BY created_at", p=pid2) == [
        {"entry_type": "PAYMENT", "reference_type": "payment", "amount": Decimal("-31.00")},
        {"entry_type": "ADJUSTMENT", "reference_type": "payment", "amount": Decimal("31.00")}]
    assert _ledger(db_engine, w["facility"]) == Decimal("31.00")
    assert _q(db_engine, "SELECT status FROM invoice WHERE id=:i", i=invoice["id"])[0]["status"] == "PARTIALLY_PAID"
    assert _q(db_engine, "SELECT COUNT(*) AS n FROM payment_allocation WHERE payment_id=:p", p=pid2)[0]["n"] == 0
    st = _q(db_engine, "SELECT * FROM state_transition WHERE subject_id=:i ORDER BY occurred_at DESC, id DESC LIMIT 1",
            i=invoice["id"])[0]
    assert (st["trigger"], st["actor_role"]) == ("UNALLOCATE_PARTIAL", "SYSTEM")
    assert st["notes"]["via"] == "phoffice" and st["notes"]["reversed_payment"] == pid2
    # applied once only
    r, _ = _event(client, "payment.reversed", "customer_account", cid, rev)
    assert r.json()["processing"] == "SKIPPED" and _ledger(db_engine, w["facility"]) == Decimal("31.00")
    # reverse the first one too: PARTIALLY_PAID -> AWAITING_PAYMENT, full exposure back
    r, _ = _event(client, "payment.reversed", "customer_account", cid,
                  {"office_payment_id": pays[0]["office_payment_id"], "amount": "20.00", "reason": "x"})
    assert r.json()["processing"] == "PROCESSED"
    assert _q(db_engine, "SELECT status FROM invoice WHERE id=:i", i=invoice["id"])[0]["status"] == "AWAITING_PAYMENT"
    assert _ledger(db_engine, w["facility"]) == Decimal("51.00")
    # a payment Store never mirrored: SKIPPED, not FAILED
    r, _ = _event(client, "payment.reversed", "customer_account", cid,
                  {"office_payment_id": "bo_pay_unknown", "amount": "1.00", "reason": "x"})
    assert r.json()["processing"] == "SKIPPED"


# ------------------------------------------------------------------ REVIEW F-5: R-057 on order.picked

def test_order_picked_refuses_a_seal_already_dispatched(client, db_engine, office_on):
    """REVIEW F-5: order.picked runs the same R-057 check as the pick route —
    a reused seal fails the event with a message naming the seal (not an
    IntegrityError at DISPATCH), and nothing is written on the line."""
    w = _world(db_engine)
    o1, _ = _office_order_to_invoice(client, db_engine, w, qty_a=1, qty_b=1)
    used = f"SEAL-{o1}-0"
    assert _q(db_engine, "SELECT COUNT(*) AS n FROM traceability_event WHERE seal_id=:s AND event_type='DISPATCHED'",
              s=used)[0]["n"] == 1
    o2 = _checkout(client, w, qty_a=1, qty_b=1).json()["orders"][0]["id"]
    la, lb = _order_lines(db_engine, o2)
    assert _event(client, "order.accepted", "order", o2, {"store_order_id": o2, "lines": [
        {"store_order_line_id": la["id"], "confirmed_qty": 1},
        {"store_order_line_id": lb["id"], "confirmed_qty": 1}]})[0].json()["processing"] == "PROCESSED"

    def picked(seals_a, seals_b):
        return {"store_order_id": o2, "lines": [
            {"store_order_line_id": la["id"], "picked_qty": 1, "lot_number": "L-A", "expiry_date": "2027-12-31",
             "seal_ids": seals_a},
            {"store_order_line_id": lb["id"], "picked_qty": 1, "lot_number": "L-B", "expiry_date": "2027-12-31",
             "seal_ids": seals_b}]}

    # 1. a seal already dispatched on another order
    r, env = _event(client, "order.picked", "order", o2, picked([f"NEW-{o2}-A"], [used]), version=2)
    assert r.status_code == 200 and r.json()["processing"] == "FAILED", r.text
    ev = _q(db_engine, "SELECT status, error FROM integration_inbound_event WHERE event_id=:e", e=env["event_id"])[0]
    assert ev["status"] == "FAILED" and used in ev["error"] and "already in use" in ev["error"]
    assert all(l["picked_at"] is None and l["seal_ids"] in (None, []) for l in _order_lines(db_engine, o2))
    assert _q(db_engine, "SELECT COUNT(*) AS n FROM traceability_event te JOIN order_line ol ON ol.id=te.order_line_id "
                         "WHERE ol.order_id=:o", o=o2)[0]["n"] == 0
    # 2. the same seal on two lines of this order
    r, env = _event(client, "order.picked", "order", o2, picked([f"DUP-{o2}"], [f"DUP-{o2}"]), version=2)
    assert r.json()["processing"] == "FAILED"
    err = _q(db_engine, "SELECT error FROM integration_inbound_event WHERE event_id=:e", e=env["event_id"])[0]["error"]
    assert f"DUP-{o2}" in err
    # 3. a corrected event goes through, and DISPATCH then succeeds
    r, _ = _event(client, "order.picked", "order", o2, picked([f"NEW-{o2}-A"], [f"NEW-{o2}-B"]), version=2)
    assert r.json()["processing"] == "PROCESSED"
    r, _ = _event(client, "order.dispatched", "order", o2, {"store_order_id": o2}, version=3)
    assert r.json()["processing"] == "PROCESSED", r.text
    assert _q(db_engine, 'SELECT status FROM "order" WHERE id=:o', o=o2)[0]["status"] == "DISPATCHED"
