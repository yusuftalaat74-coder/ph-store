"""The cart that survives the app being closed.

Sprint «سبرنت السلة», slice A. Each test here is one of its acceptance
criteria, and the numbering follows that document so a failure points at the
sentence it breaks.

The thing being defended is not "the cart works". It is that the number on
the screen and the number on the invoice are the same number, and that
neither changes without the pharmacist being told.
"""
import re

import pytest
from sqlalchemy import text

from rova.auth.security import hash_password

SUFFIX = "cart1"
PW = "rova-demo"


@pytest.fixture()
def fx(db_engine):
    """Two pharmacies and one vendor, so tenancy can be checked as well as
    behaviour. Idempotent: the database is shared across the module."""
    with db_engine.begin() as c:
        c.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"('org_v_{SUFFIX}', 'TXV{SUFFIX}', 'Medis', 'medis', 'VENDOR'), "
            f"('org_a_{SUFFIX}', 'TXA{SUFFIX}', 'Farmacia A', 'farmacia a', 'PHARMACY'), "
            f"('org_b_{SUFFIX}', 'TXB{SUFFIX}', 'Farmacia B', 'farmacia b', 'PHARMACY') "
            "ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
            "delivery_mode, mov_amount, acceptance_mode, status) VALUES "
            f"('ven_{SUFFIX}', 'org_v_{SUFFIX}', 'MAPUTO_CIDADE', 'Medis', 'IMPORTER_WHOLESALER', "
            "'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE') ON CONFLICT DO NOTHING"))
        for tag in ("a", "b"):
            c.execute(text(
                "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, "
                "trade_name, address, latitude, longitude, status) VALUES "
                f"('pha_{tag}_{SUFFIX}', 'org_{tag}_{SUFFIX}', 'MAPUTO_CIDADE', 'A', 'F', 'X', 0, 0, "
                "'ACTIVE') ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO credit_facility (id, vendor_id, pharmacy_id, limit_amount, opening_balance, "
            f"terms_days, status) VALUES ('crf_{SUFFIX}', 'ven_{SUFFIX}', 'pha_a_{SUFFIX}', "
            "100000.00, 0, 30, 'ACTIVE') ON CONFLICT DO NOTHING"))

        # Every word here is invented on purpose. The database is shared
        # across modules, so this fixture's names end up in other modules'
        # searches: seeding a real substance made it a third paracetamol
        # vendor in `test_catalogue_import`, and the placeholder brand `PROD`
        # fuzzy-matched `produto` in `test_matching_adapter`'s deliberately
        # unmatchable line. Both failed only when the whole suite ran.
        for tag, price in (("p1", "120.00"), ("p2", "40.00"), ("p3", None)):
            c.execute(text(
                "INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size, "
                "manufacturer, category, aim_status, regulated_price, review_status, reviewer_ref, "
                "search_text) VALUES (:id, 'Cartaminol', :b, 'Comprimido', '500mg', '20', 'M', "
                "'Comprimidos', 'AUTHORISED', false, 'PUBLISHED', 'test', :s) "
                "ON CONFLICT (id) DO NOTHING"),
                {"id": f"idx_{tag}_{SUFFIX}", "b": f"CARTAMINOL {tag.upper()}",
                 "s": f"cartaminol {tag.upper()}"})
            if price is not None:
                c.execute(text(
                    "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, "
                    "qty_available, pack_size, expiry_horizon_days, price, list_price, "
                    "stock_confirmed_at) VALUES (:id, :v, :p, false, 500, '1', 400, :price, "
                    ":listed, now()) ON CONFLICT (id) DO NOTHING"),
                    {"id": f"ofr_{tag}_{SUFFIX}", "v": f"ven_{SUFFIX}", "p": f"idx_{tag}_{SUFFIX}",
                     "price": price, "listed": "180.00" if tag == "p1" else None})

        def user(tag, pharmacy, phone):
            c.execute(text(
                "INSERT INTO app_user (id, phone, name, password_hash) VALUES (:u, :ph, 'U', :h) "
                "ON CONFLICT DO NOTHING"),
                {"u": f"usr_{tag}_{SUFFIX}", "ph": phone, "h": hash_password(PW)})
            c.execute(text(
                "INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES "
                "(:m, :u, :o, :r) ON CONFLICT DO NOTHING"),
                {"m": f"mem_{tag}_{SUFFIX}", "u": f"usr_{tag}_{SUFFIX}",
                 "o": f"org_{pharmacy}_{SUFFIX}", "r": ["PharmacyBuyer"]})

        user("a1", "a", "+2588491001")
        user("a2", "a", "+2588491002")     # same pharmacy, second phone
        user("b1", "b", "+2588491003")     # a different pharmacy entirely
    return {"a1": "+2588491001", "a2": "+2588491002", "b1": "+2588491003"}


def _h(client, phone):
    r = client.post("/v1/auth/login", json={"phone": phone, "password": PW, "surface": "PH"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture(autouse=True)
def _empty_cart(db_engine, fx):
    """One cart per pharmacy is the product rule, which makes it shared state
    between tests. Each test starts from an empty one so a failure points at
    its own behaviour and not at what ran before it."""
    with db_engine.begin() as c:
        c.execute(text(
            "DELETE FROM request WHERE pharmacy_id IN (:a, :b) AND status='DRAFT' "
            "AND mode='CATALOGUE' AND channel='APP'"),
            {"a": f"pha_a_{SUFFIX}", "b": f"pha_b_{SUFFIX}"})
    yield


@pytest.fixture()
def h(client, fx):
    return _h(client, fx["a1"])


def _put(client, h, product, qty):
    r = client.post("/v1/cart/lines", headers=h,
                    json={"lines": [{"index_product_id": product, "qty_requested": qty}]})
    assert r.status_code == 200, r.text
    return r.json()


P1 = f"idx_p1_{SUFFIX}"
P2 = f"idx_p2_{SUFFIX}"
P3 = f"idx_p3_{SUFFIX}"     # no offer at all


# ── 1..9 the cart ─────────────────────────────────────────────────────────

def test_1_first_line_opens_one_draft_cart(client, h, db_engine):
    cart = _put(client, h, P1, 2)
    assert cart["status"] == "DRAFT"
    assert len(cart["lines"]) == 1
    with db_engine.connect() as c:
        row = c.execute(text("SELECT mode, channel FROM request WHERE id=:r"),
                        {"r": cart["request_id"]}).mappings().one()
    assert (row["mode"], row["channel"]) == ("CATALOGUE", "APP")


def test_2_setting_a_quantity_twice_does_not_make_two_lines(client, h):
    """The phone sends the number it is showing, so a retry after a dropped
    connection must land on that number — not add to it."""
    _put(client, h, P1, 3)
    cart = _put(client, h, P1, 5)
    lines = [l for l in cart["lines"] if l["index_product_id"] == P1]
    assert len(lines) == 1
    assert lines[0]["qty_requested"] == 5


def test_3_the_cart_belongs_to_the_pharmacy_not_the_phone(client, fx):
    first = _h(client, fx["a1"])
    cart = _put(client, first, P1, 2)
    second = _h(client, fx["a2"])
    seen = client.get("/v1/cart", headers=second).json()
    assert seen["request_id"] == cart["request_id"]
    assert [l["id"] for l in seen["lines"]] == [l["id"] for l in cart["lines"]]


def test_4_another_pharmacy_sees_nothing_of_it(client, fx):
    _put(client, _h(client, fx["a1"]), P1, 2)
    other = client.get("/v1/cart", headers=_h(client, fx["b1"])).json()
    assert other["lines"] == []
    assert other["request_id"] is None


def test_5_lines_move_only_while_the_request_is_a_draft(client, h, db_engine):
    cart = _put(client, h, P1, 1)
    line_id = cart["lines"][0]["id"]
    with db_engine.begin() as c:
        c.execute(text("UPDATE request SET status='CONFIRMED' WHERE id=:r"),
                  {"r": cart["request_id"]})
    r = client.patch(f"/v1/request-lines/{line_id}", headers=h, json={"qty_requested": 4})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GUARD_FAILED"
    with db_engine.begin() as c:      # leave the module's data as we found it
        c.execute(text("UPDATE request SET status='DRAFT' WHERE id=:r"),
                  {"r": cart["request_id"]})


def test_6_every_money_field_is_a_two_decimal_string(client, h):
    cart = _put(client, h, P1, 2)
    money = re.compile(r"^\d+\.\d{2}$")
    assert money.match(cart["totals"]["goods_total"])
    for line in cart["lines"]:
        for field in ("price_seen", "price_now", "list_price"):
            if line[field] is not None:
                assert isinstance(line[field], str), field
                assert money.match(line[field]), (field, line[field])


def test_7_the_cart_price_is_the_price_the_order_is_written_at(client, h, db_engine):
    """The whole point of the shared pricing function. A cart that shows one
    number and bills another is the defect this sprint exists to prevent."""
    cart = _put(client, h, P1, 2)
    shown = {l["id"]: l["price_now"] for l in cart["lines"]}
    r = client.post(f"/v1/requests/{cart['request_id']}/checkout",
                    headers={**h, "Idempotency-Key": f"ck-{cart['request_id']}"},
                    json={"acknowledged_prices": shown})
    assert r.status_code == 200, r.text
    with db_engine.connect() as c:
        rows = c.execute(text(
            "SELECT request_line_id, unit_price FROM order_line WHERE request_line_id = ANY(:ids)"),
            {"ids": list(shown)}).mappings().all()
    assert rows, "the order wrote no lines"
    for row in rows:
        assert str(row["unit_price"]) == shown[row["request_line_id"]]


def test_8_signing_out_does_not_empty_the_cart(client, fx):
    first = _h(client, fx["a1"])
    cart = _put(client, first, P2, 3)
    again = client.get("/v1/cart", headers=_h(client, fx["a1"])).json()
    assert again["request_id"] == cart["request_id"]
    assert len(again["lines"]) == len(cart["lines"])


def test_9_product_names_come_through_untranslated(client, h):
    cart = _put(client, h, P1, 1)
    line = cart["lines"][0]
    assert line["brand_name"] == "CARTAMINOL P1"
    assert line["inn"] == "Cartaminol"


# ── 10..14 prices ─────────────────────────────────────────────────────────

def test_10_a_price_that_moves_is_reported_not_applied(client, h, db_engine):
    cart = _put(client, h, P1, 1)
    line_id = cart["lines"][0]["id"]
    seen = cart["lines"][0]["price_seen"]

    with db_engine.begin() as c:
        c.execute(text("UPDATE vendor_offer SET price='140.00' WHERE id=:o"),
                  {"o": f"ofr_p1_{SUFFIX}"})
    after = client.get("/v1/cart", headers=h).json()
    line = next(l for l in after["lines"] if l["id"] == line_id)
    assert line["price_state"] == "UP"
    assert line["price_seen"] == seen, "what the pharmacist saw must not be rewritten"
    assert line["price_now"] == "140.00"

    with db_engine.begin() as c:
        c.execute(text("UPDATE vendor_offer SET price='120.00' WHERE id=:o"),
                  {"o": f"ofr_p1_{SUFFIX}"})


def test_11_checkout_refuses_a_price_that_no_longer_matches(client, h, db_engine):
    cart = _put(client, h, P1, 1)
    stale = {l["id"]: "1.00" for l in cart["lines"]}
    r = client.post(f"/v1/requests/{cart['request_id']}/checkout",
                    headers={**h, "Idempotency-Key": f"ck-{cart['request_id']}"},
                    json={"acknowledged_prices": stale})
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "PRICE_MOVED"
    assert body["details"][0]["field"] in stale
    with db_engine.connect() as c:
        n = c.execute(text('SELECT count(*) FROM "order" WHERE request_id=:r'),
                      {"r": cart["request_id"]}).scalar()
    assert n == 0, "a refused checkout must not leave an order behind"


def test_12_a_cart_whose_price_has_not_moved_just_checks_out(client, h):
    """The common case has to stay frictionless: the price on the screen is
    still the price on the invoice, so there is nothing to ask about."""
    cart = _put(client, h, P1, 1)
    r = client.post(f"/v1/requests/{cart['request_id']}/checkout",
                    headers={**h, "Idempotency-Key": f"ck-{cart['request_id']}"}, json={})
    assert r.status_code == 200, r.text


def test_12a_a_price_that_moved_since_it_was_shown_is_refused(client, h, db_engine):
    """The guarantee, stated as a test: nobody is billed a number they were
    not shown. The claim is `price_seen` unless a newer one is sent."""
    cart = _put(client, h, P1, 1)
    line = next(l for l in cart["lines"] if l["index_product_id"] == P1)
    assert line["price_seen"] == "120.00"
    with db_engine.begin() as c:
        c.execute(text("UPDATE vendor_offer SET price=155.00 WHERE id=:o"),
                  {"o": f"ofr_p1_{SUFFIX}"})
    try:
        r = client.post(f"/v1/requests/{cart['request_id']}/checkout",
                        headers={**h, "Idempotency-Key": f"ck-{cart['request_id']}"}, json={})
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "PRICE_MOVED"
        assert [d["field"] for d in err["details"]] == [line["id"]]
        # and the pharmacist agreeing to the new number lets it through
        ok = client.post(f"/v1/requests/{cart['request_id']}/checkout",
                         headers={**h, "Idempotency-Key": f"ck2-{cart['request_id']}"},
                         json={"acknowledged_prices": {line["id"]: "155.00"}})
        assert ok.status_code == 200, ok.text
    finally:
        with db_engine.begin() as c:
            c.execute(text("UPDATE vendor_offer SET price=120.00 WHERE id=:o"),
                      {"o": f"ofr_p1_{SUFFIX}"})


def test_12b_a_request_that_showed_no_price_is_not_asked_to_confirm_one(client, h, db_engine):
    """Lines built through `POST /requests` + `/lines` never displayed a
    price, so there is no claim to contradict and nothing to acknowledge."""
    cart = _put(client, h, P2, 1)
    with db_engine.begin() as c:
        c.execute(text("UPDATE request_line SET price_seen=NULL, vendor_seen_id=NULL "
                       "WHERE request_id=:r"), {"r": cart["request_id"]})
        c.execute(text("UPDATE vendor_offer SET price=44.00 WHERE id=:o"),
                  {"o": f"ofr_p2_{SUFFIX}"})
    try:
        r = client.post(f"/v1/requests/{cart['request_id']}/checkout",
                        headers={**h, "Idempotency-Key": f"ck-{cart['request_id']}"}, json={})
        assert r.status_code == 200, r.text
    finally:
        with db_engine.begin() as c:
            c.execute(text("UPDATE vendor_offer SET price=40.00 WHERE id=:o"),
                      {"o": f"ofr_p2_{SUFFIX}"})


def test_12c_the_whatsapp_lane_still_needs_no_acknowledgement(client, h, db_engine):
    """That lane enters from CONFIRMED through a screen that shows no prices,
    so demanding agreement there would demand agreement to nothing."""
    cart = _put(client, h, P2, 1)
    with db_engine.begin() as c:
        c.execute(text("UPDATE request SET status='CONFIRMED', channel='WHATSAPP_TEXT' WHERE id=:r"),
                  {"r": cart["request_id"]})
    r = client.post(f"/v1/requests/{cart['request_id']}/checkout",
                    headers={**h, "Idempotency-Key": f"ck-{cart['request_id']}"}, json={})
    assert r.status_code == 200, r.text


def test_13_a_line_with_no_offer_is_marked_and_kept(client, h):
    cart = _put(client, h, P3, 1)
    line = next(l for l in cart["lines"] if l["index_product_id"] == P3)
    assert line["price_state"] == "NO_OFFER"
    assert line["price_now"] is None
    assert [b["line_id"] for b in cart["checkout_blocked_by"]] == [line["id"]]


def test_14_checkout_names_the_line_it_cannot_price(client, h):
    cart = _put(client, h, P3, 1)
    shown = {l["id"]: l["price_now"] for l in cart["lines"] if l["price_now"]}
    r = client.post(f"/v1/requests/{cart['request_id']}/checkout",
                    headers={**h, "Idempotency-Key": f"ck-{cart['request_id']}"},
                    json={"acknowledged_prices": shown})
    assert r.status_code == 409
    fields = [d["field"] for d in r.json()["error"]["details"]]
    blocked = [b["line_id"] for b in cart["checkout_blocked_by"]]
    assert set(blocked) <= set(fields)


# ── 15..16 the cart is not an order ───────────────────────────────────────

def test_15_the_cart_is_not_counted_as_an_open_request(client, h):
    _put(client, h, P1, 1)
    _put(client, h, P2, 2)
    summary = client.get("/v1/assistant/summary", headers=h).json()
    assert summary["cart_lines"] == 2
    cart = client.get("/v1/cart", headers=h).json()
    listed = client.get("/v1/requests", headers=h, params={"include_cart": False}).json()
    assert cart["request_id"] not in [x["id"] for x in listed["items"]]


def test_16_the_order_list_can_still_ask_for_it(client, h):
    """`include_cart` defaults to True so nothing that already calls this
    endpoint changes shape underneath it."""
    cart = _put(client, h, P1, 1)
    listed = client.get("/v1/requests", headers=h).json()
    assert cart["request_id"] in [x["id"] for x in listed["items"]]
