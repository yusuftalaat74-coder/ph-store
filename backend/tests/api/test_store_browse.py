"""The storefront: shelves, search and a product page with named vendors.

These exist because the first version of `/shelves` and `/search` answered a
bare `500` against the live catalogue: `ORDER BY (best_price IS NULL)` names
a select alias inside an expression, which PostgreSQL rejects. That is the
same shape of defect as the `enum -> 500` found in September — a query that
is only exercised with real data fails where a hand-made fixture never
reaches. So each of these drives the real SQL, not a mock.
"""
import pytest
from sqlalchemy import text

from rova.auth.security import hash_password

SUFFIX = "store1"


@pytest.fixture()
def fx(db_engine):
    """`db_engine` is shared across the module, so every insert here is
    idempotent: the fixture runs once per test and must not collide with
    itself."""
    with db_engine.begin() as c:
        c.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"('org_v1_{SUFFIX}', 'TX1{SUFFIX}', 'Medimport', 'medimport', 'VENDOR'), "
            f"('org_v2_{SUFFIX}', 'TX2{SUFFIX}', 'Medis', 'medis', 'VENDOR'), "
            f"('org_p_{SUFFIX}',  'TX3{SUFFIX}', 'Farmacia', 'farmacia', 'PHARMACY') ON CONFLICT DO NOTHING"))
        for n, org in (("1", "v1"), ("2", "v2")):
            c.execute(text(
                "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, "
                "vendor_type, delivery_mode, mov_amount, acceptance_mode, status) VALUES "
                f"('ven_{n}_{SUFFIX}', 'org_{org}_{SUFFIX}', 'MAPUTO_CIDADE', "
                f"'{'Medimport' if n == '1' else 'Medis'}', 'IMPORTER_WHOLESALER', "
                "'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE') ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, "
            "trade_name, address, latitude, longitude, status) VALUES "
            f"('pha_{SUFFIX}', 'org_p_{SUFFIX}', 'MAPUTO_CIDADE', 'A', 'F', 'X', 0, 0, 'ACTIVE') "
                "ON CONFLICT DO NOTHING"))

        # one product both vendors carry, one only Medimport has, one nobody
        # offers — the third must never reach a shelf.
        for pid, brand, maker in (("idx_pa_" + SUFFIX, "PANADO 500", "Adcock Ingram"),
                                  ("idx_ac_" + SUFFIX, "ACARBOSE", "Bluepharma"),
                                  ("idx_no_" + SUFFIX, "SEM OFERTA", "Bluepharma")):
            c.execute(text(
                "INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size, "
                "manufacturer, aim_status, regulated_price, review_status, reviewer_ref, search_text) "
                "VALUES (:id, 'Paracetamol', :b, 'Comprimido', '500mg', '20', :m, 'AUTHORISED', "
                "false, 'PUBLISHED', 'test', :s) ON CONFLICT (id) DO NOTHING"),
                {"id": pid, "b": brand, "m": maker, "s": (brand + " paracetamol").lower()})

        for oid, ven, prod, price in (
                ("ofr_1a_" + SUFFIX, "1", "idx_pa_", "838.00"),
                ("ofr_2a_" + SUFFIX, "2", "idx_pa_", "812.00"),
                ("ofr_1b_" + SUFFIX, "1", "idx_ac_", "704.00")):
            c.execute(text(
                "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, "
                "qty_available, pack_size, expiry_horizon_days, price, stock_confirmed_at) "
                f"VALUES (:id, 'ven_{ven}_{SUFFIX}', :p, false, 100, '1', 400, :price, now()) "
                "ON CONFLICT (id) DO NOTHING"),
                {"id": oid, "p": prod + SUFFIX, "price": price})

        c.execute(text(
            "INSERT INTO app_user (id, phone, name, password_hash) VALUES "
            f"('usr_{SUFFIX}', '+2588477{SUFFIX[-1]}01', 'B', :h) ON CONFLICT DO NOTHING"),
            {"h": hash_password("rova-demo")})
        c.execute(text(
            "INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES "
            f"('mem_{SUFFIX}', 'usr_{SUFFIX}', 'org_p_{SUFFIX}', :r) ON CONFLICT DO NOTHING"),
            {"r": ["PharmacyBuyer"]})
    return {"phone": f"+2588477{SUFFIX[-1]}01"}


@pytest.fixture()
def h(client, fx):
    r = client.post("/v1/auth/login",
                    json={"phone": fx["phone"], "password": "rova-demo", "surface": "PH"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def test_shelves_are_makers_with_something_to_sell(client, h):
    r = client.get("/v1/catalogue/shelves", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    shelves = {x["shelf"]: x["product_count"] for x in body["items"]}
    assert shelves.get("Adcock Ingram") == 1
    assert shelves.get("Bluepharma") == 1, "the product nobody offers must not be counted"
    assert body["products_with_offers"] >= 2


def test_search_carries_the_cheapest_price_as_a_string(client, h):
    """Rule 1 of the client: money crosses the wire as a 2dp string. A JSON
    number here would be a float by the time it reached the screen."""
    r = client.get("/v1/catalogue/search", headers=h,
                   params={"q": "panado", "in_stock": True})
    assert r.status_code == 200, r.text
    # other modules share this database and also sell paracetamol, so the
    # assertion is about this fixture's product, not about a global count
    items = [x for x in r.json()["items"] if x["id"] == f"idx_pa_{SUFFIX}"]
    assert len(items) == 1
    assert items[0]["best_price"] == "812.00", "the cheaper of the two vendors"
    assert isinstance(items[0]["best_price"], str)
    assert items[0]["fresh_offer_count"] == 2


def test_in_stock_hides_what_cannot_be_ordered(client, h):
    mine = {f"idx_pa_{SUFFIX}", f"idx_ac_{SUFFIX}", f"idx_no_{SUFFIX}"}
    with_all = {x["id"] for x in client.get(
        "/v1/catalogue/search", headers=h,
        params={"q": "paracetamol", "limit": 100}).json()["items"]} & mine
    only_live = {x["id"] for x in client.get(
        "/v1/catalogue/search", headers=h,
        params={"q": "paracetamol", "in_stock": True, "limit": 100}).json()["items"]} & mine
    assert with_all == mine
    assert only_live == mine - {f"idx_no_{SUFFIX}"}


def test_a_shelf_lists_only_its_own_maker(client, h):
    r = client.get("/v1/catalogue/shelf", headers=h, params={"name": "Adcock Ingram"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shelf"] == "Adcock Ingram"
    assert [x["brand_name"] for x in body["items"]] == ["PANADO 500"]


def test_paging_reports_whether_more_exists(client, h):
    first = client.get("/v1/catalogue/search", headers=h,
                       params={"q": "paracetamol", "limit": 1}).json()
    assert len(first["items"]) == 1
    assert first["next_offset"] == 1

    last = client.get("/v1/catalogue/search", headers=h,
                      params={"q": "paracetamol", "limit": 50}).json()
    assert last["next_offset"] is None


def test_the_product_page_names_the_vendor(client, h):
    """An opaque `ven_1_store1` on screen is worse than no vendor at all: the
    pharmacist cannot tell who they are about to owe money to."""
    r = client.get(f"/v1/catalogue/products/idx_pa_{SUFFIX}", headers=h)
    assert r.status_code == 200, r.text
    offers = r.json()["offers"]
    assert len(offers) == 2
    assert {o["vendor_name"] for o in offers} == {"Medimport", "Medis"}
    assert offers[0]["price"] == "812.00", "cheapest first"


def test_a_product_nobody_offers_still_opens(client, h):
    """It has no price and no vendor, and it must say so rather than 500."""
    r = client.get(f"/v1/catalogue/products/idx_no_{SUFFIX}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["offers"] == []
