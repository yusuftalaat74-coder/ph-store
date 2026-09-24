"""R-122 was the other wall.

A basket over the pharmacy's `buyer_approval_threshold` had every order
written and then rolled back by `SUBMIT_CART`'s own guard, with "requires
PharmacyAdmin approval" and no way to get it. SM-01 has had the state, the
transitions and the timer since it was written; nothing drove them.

These are the rules that had to hold once something did: no order exists
while a basket is waiting, the admin sees what he is approving priced the
way it will be billed, approving is not the same act as buying, and silence
sends the basket back to the buyer rather than stranding it.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from rova.auth.security import hash_password
from rova.core.clock import now
from rova.jobs import tick as jobs

SUFFIX = "adm1"
PW = "rova-demo"
P1 = f"idx_a1_{SUFFIX}"
PRICE = "400.00"
THRESHOLD = "500.00"

PEOPLE = [
    ("buyer", ["PharmacyBuyer"], "+2588493001"),
    ("admin", ["PharmacyAdmin"], "+2588493002"),
]


@pytest.fixture()
def fx(db_engine):
    with db_engine.begin() as c:
        c.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"('org_v_{SUFFIX}', 'TXV{SUFFIX}', 'Adverse', 'adverse', 'VENDOR'), "
            f"('org_p_{SUFFIX}', 'TXP{SUFFIX}', 'Farmacia A', 'farmacia a', 'PHARMACY') "
            "ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
            "delivery_mode, mov_amount, acceptance_mode, status) VALUES "
            f"('ven_{SUFFIX}', 'org_v_{SUFFIX}', 'MAPUTO_CIDADE', 'Adverse', 'IMPORTER_WHOLESALER', "
            "'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE') ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, "
            "trade_name, address, latitude, longitude, status, buyer_approval_threshold) VALUES "
            f"('pha_{SUFFIX}', 'org_p_{SUFFIX}', 'MAPUTO_CIDADE', 'A', 'F', 'X', 0, 0, 'ACTIVE', "
            f"{THRESHOLD}) ON CONFLICT (id) DO UPDATE SET buyer_approval_threshold = {THRESHOLD}"))
        c.execute(text(
            "INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size, "
            "manufacturer, category, aim_status, regulated_price, review_status, reviewer_ref, "
            "search_text) VALUES "
            f"('{P1}', 'Admitol', 'ADMITOL', 'Comprimido', '1mg', '1', 'M', 'Comprimidos', "
            "'AUTHORISED', false, 'PUBLISHED', 'test', 'admitol') ON CONFLICT (id) DO NOTHING"))
        c.execute(text(
            "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, "
            "qty_available, pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES "
            f"('ofr_{SUFFIX}', 'ven_{SUFFIX}', '{P1}', false, 900, '1', 400, {PRICE}, now()) "
            "ON CONFLICT (id) DO NOTHING"))
        for tag, roles, phone in PEOPLE:
            c.execute(text(
                "INSERT INTO app_user (id, phone, name, password_hash) VALUES (:u, :p, 'U', :h) "
                "ON CONFLICT DO NOTHING"),
                {"u": f"usr_{tag}_{SUFFIX}", "p": phone, "h": hash_password(PW)})
            c.execute(text(
                "INSERT INTO membership (id, user_id, organisation_id, role_codes) "
                "VALUES (:m, :u, :o, :r) ON CONFLICT DO NOTHING"),
                {"m": f"mem_{tag}_{SUFFIX}", "u": f"usr_{tag}_{SUFFIX}",
                 "o": f"org_p_{SUFFIX}", "r": roles})
    return {tag: phone for tag, _, phone in PEOPLE}


def _h(client, phone):
    r = client.post("/v1/auth/login", json={"phone": phone, "password": PW, "surface": "PH"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture()
def buyer(client, fx):
    return _h(client, fx["buyer"])


@pytest.fixture()
def admin(client, fx):
    return _h(client, fx["admin"])


def _basket(client, headers, db_engine, qty):
    with db_engine.begin() as c:
        c.execute(text("UPDATE request SET status='CANCELLED' WHERE is_cart AND status='DRAFT' "
                       f"AND pharmacy_id='pha_{SUFFIX}'"))
    r = client.post("/v1/cart/lines", headers=headers, json={
        "lines": [{"index_product_id": P1, "qty_requested": qty}]})
    assert r.status_code == 200, r.text
    return r.json()["request_id"]


def _orders(db_engine, request_id):
    with db_engine.begin() as c:
        return c.execute(text('SELECT count(*) FROM "order" WHERE request_id=:r'),
                         {"r": request_id}).scalar()


# ── under and over ────────────────────────────────────────────────────────

def test_a_basket_under_the_threshold_just_goes_through(client, buyer, db_engine):
    rid = _basket(client, buyer, db_engine, 1)          # 400 against 500
    r = client.post(f"/v1/requests/{rid}/checkout",
                    headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})
    assert r.status_code == 200, r.text
    assert r.json()["awaiting_admin_approval"] is False
    assert r.json()["orders"]


def test_over_the_threshold_waits_for_the_admin_and_buys_nothing(client, buyer, db_engine):
    """And it is not an error. Raising would roll back the very transition
    that sends the basket to the admin, which is how this came to be a wall
    with the approval it named unreachable."""
    rid = _basket(client, buyer, db_engine, 3)          # 1200 against 500
    r = client.post(f"/v1/requests/{rid}/checkout",
                    headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["awaiting_admin_approval"] is True
    assert body["orders"] == []
    assert body["basket_total"] == "1200.00"
    assert body["threshold"] == "500.00"
    assert _orders(db_engine, rid) == 0, "an order exists for a basket nobody approved"

    with db_engine.begin() as c:
        status = c.execute(text("SELECT status FROM request WHERE id=:r"), {"r": rid}).scalar()
    assert status == "AWAITING_ADMIN_APPROVAL"


def test_the_admin_is_told_and_can_see_what_he_is_approving(client, buyer, admin, db_engine):
    """He is approving an amount, so he has to be shown the amount — priced
    the way it will be billed, not the way it looked when the buyer added
    it."""
    rid = _basket(client, buyer, db_engine, 3)
    client.post(f"/v1/requests/{rid}/checkout",
                headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})

    told = client.get("/v1/notifications", headers=admin).json()["items"]
    mine = [n for n in told if n["event_code"] == "N-CART-NEEDS-ADMIN"
            and (n["payload"] or {}).get("request_id") == rid]
    assert mine, "the admin was not told a basket is waiting on him"
    assert mine[0]["payload"]["basket_total"] == "1200.00"
    assert mine[0]["channel"] == "IN_APP"

    priced = client.get(f"/v1/requests/{rid}/priced", headers=admin)
    assert priced.status_code == 200, priced.text
    assert priced.json()["totals"]["goods_total"] == "1200.00"


def test_approving_is_not_the_same_act_as_buying(client, buyer, admin, db_engine):
    """`ADMIN_APPROVE` releases the basket; the order is written by a
    checkout, with prices agreed at that moment. Collapsing the two would
    have the admin buying at figures nobody looked at."""
    rid = _basket(client, buyer, db_engine, 3)
    client.post(f"/v1/requests/{rid}/checkout",
                headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})

    ok = client.post(f"/v1/requests/{rid}/admin-approve", headers=admin, json={})
    assert ok.status_code == 200, ok.text
    with db_engine.begin() as c:
        assert c.execute(text("SELECT status FROM request WHERE id=:r"),
                         {"r": rid}).scalar() == "CONFIRMED"
    assert _orders(db_engine, rid) == 0, "approving bought something on its own"

    out = client.post(f"/v1/requests/{rid}/checkout",
                      headers={**admin, "Idempotency-Key": f"ck2-{rid}"}, json={})
    assert out.status_code == 200, out.text
    assert out.json()["orders"]
    assert _orders(db_engine, rid) == 1


def test_a_buyer_cannot_approve_his_own_basket(client, buyer, db_engine):
    rid = _basket(client, buyer, db_engine, 3)
    client.post(f"/v1/requests/{rid}/checkout",
                headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})
    r = client.post(f"/v1/requests/{rid}/admin-approve", headers=buyer, json={})
    assert r.status_code == 403, r.text


def test_rejecting_gives_the_basket_back_with_its_lines(client, buyer, admin, db_engine):
    """`ADMIN_REJECT` returns it to DRAFT, which is the one state the cart
    screen can edit. A cancellation would make him type the list again."""
    rid = _basket(client, buyer, db_engine, 3)
    client.post(f"/v1/requests/{rid}/checkout",
                headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})
    r = client.post(f"/v1/requests/{rid}/admin-reject", headers=admin, json={})
    assert r.status_code == 200, r.text

    cart = client.get("/v1/cart", headers=buyer).json()
    assert cart["request_id"] == rid, "the basket did not come back to him"
    assert [l["qty_requested"] for l in cart["lines"]] == [3]


def test_an_admin_can_send_his_own_basket_and_then_release_it(client, admin, db_engine):
    """Most of a two-person pharmacy is one person. `SUBMIT_CART`'s guard
    refuses an over-threshold basket whoever holds it, and the approval
    transition used to admit only a buyer — so an admin who filled a basket
    himself could neither submit it nor send it for approval. He goes through
    the same gate now, which costs him a tap and leaves the record of who
    released it intact."""
    rid = _basket(client, admin, db_engine, 3)
    first = client.post(f"/v1/requests/{rid}/checkout",
                        headers={**admin, "Idempotency-Key": f"ck-{rid}"}, json={})
    assert first.status_code == 200, first.text
    assert first.json()["awaiting_admin_approval"] is True

    assert client.post(f"/v1/requests/{rid}/admin-approve",
                       headers=admin, json={}).status_code == 200
    out = client.post(f"/v1/requests/{rid}/checkout",
                      headers={**admin, "Idempotency-Key": f"ck2-{rid}"}, json={})
    assert out.status_code == 200, out.text
    assert out.json()["orders"]


# ── silence ───────────────────────────────────────────────────────────────

def test_an_unanswered_basket_goes_back_to_the_buyer(client, buyer, db_engine):
    """Without the job the timer expires, `_guard_admin_approve` starts
    refusing because it requires a running timer, and the basket sits where
    nobody can approve it and no screen can edit it — the same dead end,
    one state along."""
    rid = _basket(client, buyer, db_engine, 3)
    client.post(f"/v1/requests/{rid}/checkout",
                headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})
    with db_engine.begin() as c:
        c.execute(text("UPDATE sla_timer SET expires_at=:t WHERE policy_type='ADMIN_APPROVAL' "
                       "AND subject_id=:r AND status='RUNNING'"),
                  {"t": now() - timedelta(minutes=1), "r": rid})

    assert jobs.admin_approval_timeout() >= 1
    with db_engine.begin() as c:
        assert c.execute(text("SELECT status FROM request WHERE id=:r"),
                         {"r": rid}).scalar() == "DRAFT"

    cart = client.get("/v1/cart", headers=buyer).json()
    assert cart["request_id"] == rid
    assert [l["qty_requested"] for l in cart["lines"]] == [3], "his basket came back empty"

    told = client.get("/v1/notifications", headers=buyer).json()["items"]
    assert any(n["event_code"] == "N-CART-APPROVAL-TIMED-OUT"
               and (n["payload"] or {}).get("request_id") == rid for n in told)


def test_a_basket_answered_in_time_is_not_taken_back(client, buyer, admin, db_engine):
    rid = _basket(client, buyer, db_engine, 3)
    client.post(f"/v1/requests/{rid}/checkout",
                headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})
    client.post(f"/v1/requests/{rid}/admin-approve", headers=admin, json={})
    jobs.admin_approval_timeout()
    with db_engine.begin() as c:
        assert c.execute(text("SELECT status FROM request WHERE id=:r"),
                         {"r": rid}).scalar() == "CONFIRMED"
