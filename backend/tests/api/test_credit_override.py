"""R-044 was a wall.

`credit/gate.py` has always listed `ADMIN_OVERRIDE` among a blocked
sub-basket's options, and nothing implemented it — so a pharmacist with a
full basket was told "credit limit exceeded" and given nothing to press.
These tests are that option, and the rules it has to keep: one approval buys
one order, it covers an amount rather than a pharmacy, it expires, and the
name of whoever allowed it ends up on the order.
"""
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from rova.auth.security import hash_password
from rova.core.clock import now

SUFFIX = "cov1"
PW = "rova-demo"

P1 = f"idx_c1_{SUFFIX}"
LIMIT = "300.00"          # the facility
PRICE = "250.00"          # one unit


# Both sides of the table, plus one person who works for the distributor and
# still may not extend its credit.
PEOPLE = [
    ("buyer", f"org_p_{SUFFIX}", ["PharmacyBuyer"], "+2588492001"),
    ("desk", f"org_v_{SUFFIX}", ["VendorOrderDesk"], "+2588492002"),
    ("vfin", f"org_v_{SUFFIX}", ["VendorFinance"], "+2588492003"),
    ("pfin", None, ["PlatformFinance"], "+2588492004"),
    ("picker", f"org_v_{SUFFIX}", ["VendorPicker"], "+2588492005"),
]


@pytest.fixture()
def fx(db_engine):
    """A pharmacy with a small facility, a distributor, and the people on
    both sides who are allowed to answer."""
    with db_engine.begin() as c:
        c.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"('org_v_{SUFFIX}', 'TXV{SUFFIX}', 'Credis', 'credis', 'VENDOR'), "
            f"('org_p_{SUFFIX}', 'TXP{SUFFIX}', 'Farmacia C', 'farmacia c', 'PHARMACY') "
            "ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
            "delivery_mode, mov_amount, acceptance_mode, status) VALUES "
            f"('ven_{SUFFIX}', 'org_v_{SUFFIX}', 'MAPUTO_CIDADE', 'Credis', 'IMPORTER_WHOLESALER', "
            "'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE') ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, "
            "trade_name, address, latitude, longitude, status) VALUES "
            f"('pha_{SUFFIX}', 'org_p_{SUFFIX}', 'MAPUTO_CIDADE', 'A', 'F', 'X', 0, 0, 'ACTIVE') "
            "ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO credit_facility (id, vendor_id, pharmacy_id, limit_amount, "
            "opening_balance, terms_days, status) VALUES "
            f"('crf_{SUFFIX}', 'ven_{SUFFIX}', 'pha_{SUFFIX}', {LIMIT}, 0, 30, 'ACTIVE') "
            "ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size, "
            "manufacturer, category, aim_status, regulated_price, review_status, reviewer_ref, "
            "search_text) VALUES "
            f"('{P1}', 'Creditol', 'CREDITOL', 'Comprimido', '1mg', '1', 'M', 'Comprimidos', "
            "'AUTHORISED', false, 'PUBLISHED', 'test', 'creditol') ON CONFLICT (id) DO NOTHING"))
        c.execute(text(
            "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, "
            "qty_available, pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES "
            f"('ofr_{SUFFIX}', 'ven_{SUFFIX}', '{P1}', false, 500, '1', 400, {PRICE}, now()) "
            "ON CONFLICT (id) DO NOTHING"))

        for tag, org, roles, phone in PEOPLE:
            c.execute(text(
                "INSERT INTO app_user (id, phone, name, password_hash) VALUES (:u, :p, 'U', :h) "
                "ON CONFLICT DO NOTHING"),
                {"u": f"usr_{tag}_{SUFFIX}", "p": phone, "h": hash_password(PW)})
            c.execute(text(
                "INSERT INTO membership (id, user_id, organisation_id, role_codes) "
                "VALUES (:m, :u, :o, :r) ON CONFLICT DO NOTHING"),
                {"m": f"mem_{tag}_{SUFFIX}", "u": f"usr_{tag}_{SUFFIX}", "o": org, "r": roles})
    return {tag: phone for tag, _, _, phone in PEOPLE}


def _h(client, phone, surface="PH"):
    r = client.post("/v1/auth/login",
                    json={"phone": phone, "password": PW, "surface": surface})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture()
def buyer(client, fx):
    return _h(client, fx["buyer"])


@pytest.fixture()
def desk(client, fx):
    return _h(client, fx["desk"], surface="VN")


def _blocked_cart(client, buyer, db_engine, qty=2):
    """A basket worth more than the facility: two at 250 against a 300 limit."""
    with db_engine.begin() as c:
        c.execute(text("UPDATE request SET status='CANCELLED' WHERE is_cart AND status='DRAFT' "
                       f"AND pharmacy_id='pha_{SUFFIX}'"))
    r = client.post("/v1/cart/lines", headers=buyer, json={
        "lines": [{"index_product_id": P1, "qty_requested": qty}]})
    assert r.status_code == 200, r.text
    return r.json()["request_id"]


# ── the wall, and the door in it ──────────────────────────────────────────

def test_over_the_limit_is_refused_and_says_what_it_would_take(client, buyer, db_engine):
    """The refusal has to be actionable: which distributor, how much headroom
    there is, and what the order comes to. Without those three the app cannot
    say anything useful and the pharmacist is back at a wall."""
    rid = _blocked_cart(client, buyer, db_engine)
    r = client.post(f"/v1/requests/{rid}/checkout",
                    headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["rule"] == "R-044"
    fields = {d["field"]: d["reason"] for d in err["details"]}
    assert "ADMIN_OVERRIDE" in fields["options"]
    assert fields["vendor_id"] == f"ven_{SUFFIX}"
    assert fields["order_value"] == "500.00"
    assert re.fullmatch(r"-?\d+\.\d{2}", fields["headroom"])


def test_asking_is_refused_when_the_order_actually_fits(client, buyer, db_engine):
    """An approval the pharmacy did not need is headroom nobody decided to
    give it."""
    rid = _blocked_cart(client, buyer, db_engine, qty=1)   # 250 against 300
    r = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                    json={"vendor_id": f"ven_{SUFFIX}"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["rule"] == "R-044"


def test_the_whole_path_from_the_wall_to_the_order(client, buyer, desk, db_engine):
    rid = _blocked_cart(client, buyer, db_engine)

    asked = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                        json={"vendor_id": f"ven_{SUFFIX}", "note": "cliente à espera"})
    assert asked.status_code == 200, asked.text
    ask = asked.json()
    assert ask["status"] == "PENDING"
    assert ask["amount_requested"] == "500.00"
    assert ask["note"] == "cliente à espera"
    # the evidence the approver decides on, frozen at the moment of asking
    assert ask["headroom_at_request"] == "300.00"

    # the distributor's order desk sees it
    inbox = client.get("/v1/credit-overrides", headers=desk, params={"status": "PENDING"}).json()
    assert ask["id"] in [i["id"] for i in inbox["items"]]

    ok = client.post(f"/v1/credit-overrides/{ask['id']}/approve", headers=desk,
                     json={"reason": "cliente antigo, paga sempre"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "APPROVED"
    assert ok.json()["decided_by_side"] == "VENDOR"

    out = client.post(f"/v1/requests/{rid}/checkout",
                      headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})
    assert out.status_code == 200, out.text

    with db_engine.begin() as c:
        order = c.execute(text(
            'SELECT payment_terms, credit_override_user_id, credit_override_reason '
            'FROM "order" WHERE request_id=:r'), {"r": rid}).mappings().one()
    assert order["payment_terms"] == "CREDIT_N_DAYS", "it went through on credit, not cash"
    assert order["credit_override_user_id"] == f"usr_desk_{SUFFIX}", \
        "the order does not say who allowed it past the limit"
    assert order["credit_override_reason"] == "cliente antigo, paga sempre"


def test_one_approval_buys_one_order(client, buyer, desk, db_engine):
    """Otherwise it is not an approval, it is a limit raise under another
    name — and nobody agreed to raise the limit."""
    rid = _blocked_cart(client, buyer, db_engine)
    ask = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                      json={"vendor_id": f"ven_{SUFFIX}"}).json()
    client.post(f"/v1/credit-overrides/{ask['id']}/approve", headers=desk, json={})
    assert client.post(f"/v1/requests/{rid}/checkout",
                       headers={**buyer, "Idempotency-Key": f"ck-{rid}"},
                       json={}).status_code == 200

    spent = client.get("/v1/credit-overrides", headers=desk).json()["items"]
    mine = next(i for i in spent if i["id"] == ask["id"])
    assert mine["status"] == "USED"
    assert mine["used_order_id"]

    # a second basket with the same distributor hits the wall again
    rid2 = _blocked_cart(client, buyer, db_engine)
    again = client.post(f"/v1/requests/{rid2}/checkout",
                        headers={**buyer, "Idempotency-Key": f"ck-{rid2}"}, json={})
    assert again.status_code == 409
    assert again.json()["error"]["rule"] == "R-044"


def test_an_approval_does_not_cover_a_bigger_basket(client, buyer, desk, db_engine):
    """It covers an amount. If he adds more after it was granted, nobody
    approved the larger number."""
    rid = _blocked_cart(client, buyer, db_engine, qty=2)          # 500
    ask = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                      json={"vendor_id": f"ven_{SUFFIX}"}).json()
    assert ask["amount_requested"] == "500.00"
    client.post(f"/v1/credit-overrides/{ask['id']}/approve", headers=desk, json={})

    client.post("/v1/cart/lines", headers=buyer, json={
        "lines": [{"index_product_id": P1, "qty_requested": 4}]})   # now 1000
    r = client.post(f"/v1/requests/{rid}/checkout",
                    headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["rule"] == "R-044"


def test_an_approval_nobody_spent_stops_being_live(client, buyer, desk, db_engine):
    """CFG-CREDIT-OVERRIDE-HOURS. Headroom the credit team stopped thinking
    about days ago must not still be spendable."""
    rid = _blocked_cart(client, buyer, db_engine)
    ask = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                      json={"vendor_id": f"ven_{SUFFIX}"}).json()
    client.post(f"/v1/credit-overrides/{ask['id']}/approve", headers=desk, json={})
    with db_engine.begin() as c:
        c.execute(text("UPDATE credit_override SET expires_at=:t WHERE id=:i"),
                  {"t": now() - timedelta(minutes=1), "i": ask["id"]})

    r = client.post(f"/v1/requests/{rid}/checkout",
                    headers={**buyer, "Idempotency-Key": f"ck-{rid}"}, json={})
    assert r.status_code == 409
    assert r.json()["error"]["rule"] == "R-044"


def test_a_declined_request_buys_nothing(client, buyer, desk, db_engine):
    rid = _blocked_cart(client, buyer, db_engine)
    ask = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                      json={"vendor_id": f"ven_{SUFFIX}"}).json()
    no = client.post(f"/v1/credit-overrides/{ask['id']}/decline", headers=desk,
                     json={"reason": "conta em atraso"})
    assert no.status_code == 200
    assert no.json()["status"] == "DECLINED"
    assert client.post(f"/v1/requests/{rid}/checkout",
                       headers={**buyer, "Idempotency-Key": f"ck-{rid}"},
                       json={}).status_code == 409


def test_paying_cash_is_still_a_way_through(client, buyer, db_engine):
    """The other door, and the one that needs nobody's permission: the
    facility is bypassed entirely, so there is no limit to exceed."""
    rid = _blocked_cart(client, buyer, db_engine)
    r = client.post(f"/v1/requests/{rid}/checkout",
                    headers={**buyer, "Idempotency-Key": f"ck-{rid}"},
                    json={"payment_overrides": {f"ven_{SUFFIX}": "UPFRONT"}})
    assert r.status_code == 200, r.text
    with db_engine.begin() as c:
        terms = c.execute(text('SELECT payment_terms FROM "order" WHERE request_id=:r'),
                          {"r": rid}).scalar()
    assert terms == "UPFRONT"


# ── who may answer ────────────────────────────────────────────────────────

def test_the_pharmacy_cannot_approve_its_own(client, buyer, db_engine):
    """A facility a pharmacy can raise for itself is not a facility."""
    rid = _blocked_cart(client, buyer, db_engine)
    ask = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                      json={"vendor_id": f"ven_{SUFFIX}"}).json()
    r = client.post(f"/v1/credit-overrides/{ask['id']}/approve", headers=buyer, json={})
    assert r.status_code == 403, r.text


def test_a_warehouse_picker_cannot_approve(client, buyer, fx, db_engine):
    """Working for the distributor is not the same as being able to extend
    its credit."""
    rid = _blocked_cart(client, buyer, db_engine)
    ask = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                      json={"vendor_id": f"ven_{SUFFIX}"}).json()
    picker = _h(client, fx["picker"], surface="VW")
    r = client.post(f"/v1/credit-overrides/{ask['id']}/approve", headers=picker, json={})
    assert r.status_code == 403, r.text


def test_the_platform_can_answer_too_and_the_row_says_so(client, buyer, fx, db_engine):
    """Both sides may decide — the distributor extends the credit, the
    platform carries the relationship — and which of them did is recorded at
    the time rather than guessed later from memberships that will have
    changed."""
    rid = _blocked_cart(client, buyer, db_engine)
    ask = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                      json={"vendor_id": f"ven_{SUFFIX}"}).json()
    plat = _h(client, fx["pfin"], surface="OP")
    r = client.post(f"/v1/credit-overrides/{ask['id']}/approve", headers=plat,
                    json={"reason": "PH Store assume"})
    assert r.status_code == 200, r.text
    assert r.json()["decided_by_side"] == "PLATFORM"
    assert r.json()["decided_by_user_id"] == f"usr_pfin_{SUFFIX}"


def test_answering_twice_is_refused(client, buyer, desk, db_engine):
    rid = _blocked_cart(client, buyer, db_engine)
    ask = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                      json={"vendor_id": f"ven_{SUFFIX}"}).json()
    assert client.post(f"/v1/credit-overrides/{ask['id']}/approve",
                       headers=desk, json={}).status_code == 200
    second = client.post(f"/v1/credit-overrides/{ask['id']}/decline", headers=desk, json={})
    assert second.status_code == 409, second.text


def test_asking_twice_hands_back_the_one_already_open(client, buyer, db_engine):
    """He tapped twice, or a colleague asked first. Two open asks for one
    basket means an approver answers one while the other sits looking
    unanswered."""
    rid = _blocked_cart(client, buyer, db_engine)
    first = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                        json={"vendor_id": f"ven_{SUFFIX}"}).json()
    second = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                         json={"vendor_id": f"ven_{SUFFIX}"}).json()
    assert first["id"] == second["id"]


def test_both_sides_are_told_and_so_is_the_pharmacy(client, buyer, desk, db_engine):
    rid = _blocked_cart(client, buyer, db_engine)
    ask = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                      json={"vendor_id": f"ven_{SUFFIX}"}).json()
    with db_engine.begin() as c:
        asked = c.execute(text(
            "SELECT recipient_org_id, recipient_role, channel FROM notification "
            "WHERE event_code='N-CREDIT-OVERRIDE-ASKED' "
            "AND payload->>'credit_override_id' = :i"), {"i": ask["id"]}).mappings().all()
    assert {r["recipient_org_id"] for r in asked} >= {f"org_v_{SUFFIX}"}
    assert {r["recipient_role"] for r in asked} >= {"PlatformFinance"}
    assert {r["channel"] for r in asked} == {"IN_APP"}, "nothing on a channel nobody delivers"

    client.post(f"/v1/credit-overrides/{ask['id']}/approve", headers=desk, json={})
    told = client.get("/v1/notifications", headers=buyer).json()["items"]
    assert any(n["event_code"] == "N-CREDIT-OVERRIDE-APPROVED"
               and (n["payload"] or {}).get("credit_override_id") == ask["id"] for n in told)


def test_another_distributor_cannot_see_or_answer_it(client, buyer, db_engine):
    """A vendor answers for its own account and nobody else's."""
    rid = _blocked_cart(client, buyer, db_engine)
    ask = client.post(f"/v1/requests/{rid}/credit-override", headers=buyer,
                      json={"vendor_id": f"ven_{SUFFIX}"}).json()
    with db_engine.begin() as c:
        c.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) "
            f"VALUES ('org_v2_{SUFFIX}', 'TXV2{SUFFIX}', 'Outra', 'outra', 'VENDOR') "
            "ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, "
            "vendor_type, delivery_mode, mov_amount, acceptance_mode, status) VALUES "
            f"('ven2_{SUFFIX}', 'org_v2_{SUFFIX}', 'MAPUTO_CIDADE', 'Outra', "
            "'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE') "
            "ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO app_user (id, phone, name, password_hash) "
            f"VALUES ('usr_other_{SUFFIX}', '+2588492009', 'O', :h) ON CONFLICT DO NOTHING"),
            {"h": hash_password(PW)})
        c.execute(text(
            "INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES "
            f"('mem_other_{SUFFIX}', 'usr_other_{SUFFIX}', 'org_v2_{SUFFIX}', :r) "
            "ON CONFLICT DO NOTHING"), {"r": ["VendorAdmin"]})

    other = _h(client, "+2588492009", surface="VN")
    seen = client.get("/v1/credit-overrides", headers=other).json()["items"]
    assert ask["id"] not in [i["id"] for i in seen]
    r = client.post(f"/v1/credit-overrides/{ask['id']}/approve", headers=other, json={})
    assert r.status_code == 404, r.text
