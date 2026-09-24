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

        # `listed` is the vendor's own published public price. The Medimport
        # line deliberately has none, so the "no list price means no margin"
        # path is covered as well as the happy one.
        for oid, ven, prod, price, listed in (
                ("ofr_1a_" + SUFFIX, "1", "idx_pa_", "838.00", None),
                ("ofr_2a_" + SUFFIX, "2", "idx_pa_", "812.00", "1200.00"),
                ("ofr_1b_" + SUFFIX, "1", "idx_ac_", "704.00", "1043.00")):
            c.execute(text(
                "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, "
                "qty_available, pack_size, expiry_horizon_days, price, list_price, "
                "stock_confirmed_at) "
                f"VALUES (:id, 'ven_{ven}_{SUFFIX}', :p, false, 100, '1', 400, :price, "
                ":listed, now()) ON CONFLICT (id) DO NOTHING"),
                {"id": oid, "p": prod + SUFFIX, "price": price, "listed": listed})

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


def test_the_margin_comes_from_the_same_offer_as_the_price(client, h):
    """`best_price` and `list_price` must be the cheapest offer's own pair.
    Taking the lowest price from one vendor and the highest list price from
    another would manufacture a discount that nobody published."""
    r = client.get("/v1/catalogue/search", headers=h,
                   params={"q": "panado", "in_stock": True})
    item = [x for x in r.json()["items"] if x["id"] == f"idx_pa_{SUFFIX}"][0]
    assert item["best_price"] == "812.00"     # Medis
    assert item["list_price"] == "1200.00"    # Medis' own PVP, not Medimport's
    assert item["discount_pct"] == 32         # (1200 - 812) / 1200


def test_no_published_list_price_means_no_discount_not_zero(client, h):
    """Medimport publishes no public price for ACARBOSE in this fixture... but
    it does. The case that matters is the offer that has none: the field is
    null, so the card shows nothing rather than "-0%", which a pharmacist
    reads as this vendor giving them nothing."""
    r = client.get(f"/v1/catalogue/products/idx_pa_{SUFFIX}", headers=h)
    by_vendor = {o["vendor_name"]: o for o in r.json()["offers"]}
    assert by_vendor["Medimport"]["list_price"] is None
    assert by_vendor["Medimport"]["discount_pct"] is None
    assert by_vendor["Medis"]["discount_pct"] == 32


def test_the_database_refuses_a_list_price_below_the_price(db_engine):
    """A negative margin is a transcription error, not a discount, and the
    CHECK makes it unrepresentable rather than leaving the API to notice."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError
    with _pytest.raises(IntegrityError):
        with db_engine.begin() as c:
            c.execute(text(
                "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, "
                "qty_available, pack_size, expiry_horizon_days, price, list_price, "
                "stock_confirmed_at) VALUES "
                f"('ofr_bad_{SUFFIX}', 'ven_1_{SUFFIX}', 'idx_ac_{SUFFIX}', false, 1, '1', 10, "
                "500.00, 100.00, now())"))


def _ids(body):
    return {x["id"] for x in body["items"]}


def test_filters_offer_only_what_can_be_ordered(client, h, db_engine):
    """A chip that comes back empty teaches the pharmacist not to trust the
    chips, so a distributor with no live offer and a category with no offered
    product are not offered as filters at all."""
    r = client.get("/v1/catalogue/filters", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    names = {v["trade_name"]: v["product_count"] for v in body["vendors"]}
    assert names["Medimport"] == 2, "two distinct products, not three offer rows"
    assert names["Medis"] == 1
    assert all(v["product_count"] > 0 for v in body["vendors"])
    assert all(c["offered_count"] > 0 for c in body["categories"])


def test_a_category_nobody_stocks_is_not_offered_as_a_filter(client, h, db_engine):
    """The same promise on the other axis, which the first version of this
    endpoint did not keep: the live catalogue has five categories with no
    orderable product in them -- 384 in-vitro diagnostics among them -- and
    every one was offered as a chip that led to an empty screen.
    """
    with db_engine.begin() as c:
        c.execute(text(
            "INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size, "
            "manufacturer, category, aim_status, regulated_price, review_status, "
            "reviewer_ref, search_text) VALUES "
            f"('idx_unstocked_{SUFFIX}', 'Nadaol', 'NADAOL', 'Comprimido', '1mg', '1', 'M', "
            "'Categoria Sem Stock', 'AUTHORISED', false, 'PUBLISHED', 'test', 'nadaol') "
            "ON CONFLICT (id) DO NOTHING"))
    body = client.get("/v1/catalogue/filters", headers=h).json()
    offered = {c["name"] for c in body["categories"]}
    assert "Categoria Sem Stock" not in offered

    # and the chip is absent because the shelf really is empty, not because
    # the category was filtered out by accident
    found = client.get("/v1/catalogue/search", headers=h,
                       params={"category": "Categoria Sem Stock", "in_stock": True}).json()
    assert found["items"] == []


def test_a_distributor_shows_only_what_it_sells(client, h):
    only = client.get("/v1/catalogue/search", headers=h,
                      params={"vendor_id": f"ven_2_{SUFFIX}"}).json()
    assert _ids(only) == {f"idx_pa_{SUFFIX}"}, "Medis carries the one product here"


def test_a_distributor_filter_never_lists_what_it_does_not_offer(client, h):
    """`vendor_id` implies in-stock: a distributor's shelf that listed a
    product they do not carry would be a lie about who sells what."""
    body = client.get("/v1/catalogue/search", headers=h,
                      params={"vendor_id": f"ven_1_{SUFFIX}"}).json()
    assert f"idx_no_{SUFFIX}" not in _ids(body)


def test_a_category_crosses_distributors(client, h, db_engine):
    """The whole point of the category axis: it is asked regardless of who
    sells it."""
    with db_engine.begin() as c:
        c.execute(text("UPDATE index_product SET category='Comprimidos' "
                       "WHERE id IN (:a, :b)"),
                  {"a": f"idx_pa_{SUFFIX}", "b": f"idx_ac_{SUFFIX}"})
    body = client.get("/v1/catalogue/search", headers=h,
                      params={"category": "Comprimidos", "in_stock": True}).json()
    got = _ids(body)
    assert f"idx_pa_{SUFFIX}" in got and f"idx_ac_{SUFFIX}" in got


def test_the_two_filters_narrow_together(client, h, db_engine):
    with db_engine.begin() as c:
        c.execute(text("UPDATE index_product SET category='Comprimidos' "
                       "WHERE id IN (:a, :b)"),
                  {"a": f"idx_pa_{SUFFIX}", "b": f"idx_ac_{SUFFIX}"})
        c.execute(text("UPDATE index_product SET category='Tópicos' WHERE id=:a"),
                  {"a": f"idx_ac_{SUFFIX}"})
    both = client.get("/v1/catalogue/search", headers=h,
                      params={"vendor_id": f"ven_1_{SUFFIX}",
                              "category": "Comprimidos"}).json()
    assert _ids(both) == {f"idx_pa_{SUFFIX}"}, \
        "Medimport sells both, but only one of them is a tablet"


def test_search_text_and_both_filters_compose(client, h, db_engine):
    with db_engine.begin() as c:
        c.execute(text("UPDATE index_product SET category='Comprimidos' WHERE id=:a"),
                  {"a": f"idx_pa_{SUFFIX}"})
    body = client.get("/v1/catalogue/search", headers=h,
                      params={"q": "panado", "vendor_id": f"ven_2_{SUFFIX}",
                              "category": "Comprimidos"}).json()
    assert _ids(body) == {f"idx_pa_{SUFFIX}"}

    none = client.get("/v1/catalogue/search", headers=h,
                      params={"q": "panado", "vendor_id": f"ven_2_{SUFFIX}",
                              "category": "Injectáveis"}).json()
    assert none["items"] == [], "a category the product is not in must exclude it"


def test_search_with_no_query_is_a_browse_not_an_error(client, h):
    """The store opens on everything orderable; `q` is optional."""
    r = client.get("/v1/catalogue/search", headers=h, params={"in_stock": True})
    assert r.status_code == 200
    assert len(r.json()["items"]) >= 2


# ── slice C: the shelves' own rules ───────────────────────────────────────

def test_23_discount_sort_is_biggest_first(client, h, db_engine):
    """Acceptance 23. The figure is the vendor's own published public price
    against what the pharmacy pays — not a number we invented — and
    `ck_vendor_offer_list_price_above_price` (migration 0003) makes a
    negative one impossible at the database level.

    Three discounts far enough apart to prove an order. Asserting that the
    whole list is descending proves nothing when every row in the fixture
    happens to sit at the same percentage, which is what this test did
    before: it passed against 32%, 32%.
    """
    with db_engine.begin() as c:
        for tag, price, listed in (("lo", "90.00", "100.00"),    # 10%
                                   ("mid", "50.00", "100.00"),   # 50%
                                   ("hi", "20.00", "100.00")):   # 80%
            c.execute(text(
                "INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size, "
                "manufacturer, aim_status, regulated_price, review_status, reviewer_ref, "
                f"search_text) VALUES ('idx_d{tag}_{SUFFIX}', 'Descontol', :b, 'Comprimido', "
                "'1mg', '1', 'M', 'AUTHORISED', false, 'PUBLISHED', 'test', 'descontol') "
                "ON CONFLICT (id) DO NOTHING"), {"b": f"DESCONTOL {tag.upper()}"})
            c.execute(text(
                "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, "
                "qty_available, pack_size, expiry_horizon_days, price, list_price, "
                f"stock_confirmed_at) VALUES ('ofr_d{tag}_{SUFFIX}', 'ven_1_{SUFFIX}', "
                f"'idx_d{tag}_{SUFFIX}', false, 100, '1', 300, :p, :l, now()) "
                "ON CONFLICT (id) DO NOTHING"), {"p": price, "l": listed})

    body = client.get("/v1/catalogue/search", headers=h,
                      params={"sort": "discount", "in_stock": True, "limit": 100}).json()
    pcts = [i["discount_pct"] for i in body["items"]]
    assert pcts == sorted(pcts, reverse=True), pcts

    order = [i["id"] for i in body["items"]]
    hi, mid, lo = (order.index(f"idx_d{t}_{SUFFIX}") for t in ("hi", "mid", "lo"))
    assert hi < mid < lo, "80% must come before 50% before 10%"


def test_a_list_price_equal_to_the_price_is_not_a_discount(client, h, db_engine):
    """A vendor publishing a public price equal to what it charges has
    published no discount. Keeping that row would put a card with no badge on
    a shelf whose whole promise is the badge — the padding the filter exists
    to prevent, arriving through the other door."""
    with db_engine.begin() as c:
        c.execute(text(
            "INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size, "
            "manufacturer, aim_status, regulated_price, review_status, reviewer_ref, "
            f"search_text) VALUES ('idx_dzero_{SUFFIX}', 'Zerol', 'ZEROL', 'Comprimido', "
            "'1mg', '1', 'M', 'AUTHORISED', false, 'PUBLISHED', 'test', 'zerol') "
            "ON CONFLICT (id) DO NOTHING"))
        c.execute(text(
            "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, "
            "qty_available, pack_size, expiry_horizon_days, price, list_price, "
            f"stock_confirmed_at) VALUES ('ofr_dzero_{SUFFIX}', 'ven_1_{SUFFIX}', "
            f"'idx_dzero_{SUFFIX}', false, 100, '1', 300, 70.00, 70.00, now()) "
            "ON CONFLICT (id) DO NOTHING"))

    body = client.get("/v1/catalogue/search", headers=h,
                      params={"sort": "discount", "in_stock": True, "limit": 100}).json()
    assert f"idx_dzero_{SUFFIX}" not in [i["id"] for i in body["items"]]
    # and it is still an ordinary product everywhere else
    plain = client.get("/v1/catalogue/search", headers=h,
                       params={"q": "zerol", "limit": 10}).json()
    assert f"idx_dzero_{SUFFIX}" in [i["id"] for i in plain["items"]]


def test_24_no_row_on_the_discount_shelf_has_a_discount_without_a_list_price(client, h):
    """Acceptance 24. A "biggest discount" shelf whose tail is products with
    no published list price would be padding a promise with rows that do not
    keep it."""
    body = client.get("/v1/catalogue/search", headers=h,
                      params={"sort": "discount", "in_stock": True, "limit": 50}).json()
    assert body["items"], "nothing to check"
    for i in body["items"]:
        assert i["list_price"] is not None, i["id"]
        assert i["discount_pct"] is not None, i["id"]


def test_the_shelves_fetch_their_cards_by_id_and_keep_the_caller_s_order(client, h):
    """`assistant/*` answers in product ids; the card is this endpoint's
    shape. The rows come back as a set, so the *caller* orders them — how
    often a pharmacy reorders something is not a property of the catalogue.
    """
    everything = client.get("/v1/catalogue/search", headers=h,
                            params={"in_stock": True, "limit": 3}).json()["items"]
    ids = [i["id"] for i in everything]
    assert len(ids) >= 2, "the fixture needs at least two offered products"

    body = client.get("/v1/catalogue/search", headers=h,
                      params={"ids": ",".join(reversed(ids)), "limit": 50}).json()
    assert {i["id"] for i in body["items"]} == set(ids)
    # same row shape as a search result, so the store renders both with one
    # piece of code
    assert set(body["items"][0]) == set(everything[0])


def test_an_unknown_id_is_simply_absent_rather_than_an_error(client, h):
    body = client.get("/v1/catalogue/search", headers=h,
                      params={"ids": "idx_not_a_real_product", "limit": 10}).json()
    assert body["items"] == []


def test_savings_reports_a_product_once_at_the_price_he_paid_most_recently(client, h, db_engine):
    """Per order line, this endpoint emitted a product as many times as it
    had been bought — and, worse, emitted only the lines that its own filter
    liked, so the line that contradicted the card was the one the caller
    never saw: paid 120 in July, 95 last week, 100 today, and the card said
    "you paid 120, now 100"."""
    with db_engine.begin() as c:
        c.execute(text(
            "INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size, "
            "manufacturer, aim_status, regulated_price, review_status, reviewer_ref, "
            f"search_text) VALUES ('idx_sv_{SUFFIX}', 'Savol', 'SAVOL', 'Comprimido', '1mg', "
            "'1', 'M', 'AUTHORISED', false, 'PUBLISHED', 'test', 'savol') "
            "ON CONFLICT (id) DO NOTHING"))
        # today's cheapest fresh offer: 100
        c.execute(text(
            "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, "
            "qty_available, pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES "
            f"('ofr_sv_{SUFFIX}', 'ven_1_{SUFFIX}', 'idx_sv_{SUFFIX}', false, 50, '1', 300, "
            "100.00, now()) ON CONFLICT (id) DO NOTHING"))
        c.execute(text(
            "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, "
            "allocation_strategy) VALUES "
            f"('req_sv_{SUFFIX}', 'RQ-2026-880001', 'pha_{SUFFIX}', 'CATALOGUE', 'APP', "
            "'IN_FULFILMENT', 'FEWEST_VENDORS') ON CONFLICT (id) DO NOTHING"))
        for tag, price, days in (("old", "120.00", 60), ("new", "95.00", 9)):
            c.execute(text(
                'INSERT INTO "order" (id, number, request_id, pharmacy_id, vendor_id, status, '
                "payment_terms, delivery_mode, goods_total, created_at) VALUES "
                f"(:o, :n, 'req_sv_{SUFFIX}', 'pha_{SUFFIX}', 'ven_1_{SUFFIX}', 'CLOSED', "
                "'UPFRONT', 'VENDOR_OWN_FLEET', 0, now() - make_interval(days => :d)) "
                "ON CONFLICT (id) DO NOTHING"),
                {"o": f"ord_sv_{tag}_{SUFFIX}", "n": f"OR-2026-88{days:03d}", "d": days})
            c.execute(text(
                "INSERT INTO request_line (id, request_id, index_product_id, qty_requested, "
                "line_kind, match_status) VALUES "
                f"(:rl, 'req_sv_{SUFFIX}', 'idx_sv_{SUFFIX}', 1, 'CATALOGUE', 'RESOLVED') "
                "ON CONFLICT (id) DO NOTHING"),
                {"rl": f"rql_sv_{tag}_{SUFFIX}"})
            c.execute(text(
                "INSERT INTO order_line (id, order_id, request_line_id, index_product_id, "
                "regulated_price, ordered_qty, confirmed_qty, unit_price, price_source, "
                "price_source_id) VALUES "
                f"(:l, :o, :rl, 'idx_sv_{SUFFIX}', false, 1, 1, :p, 'VENDOR_OFFER', "
                f"'ofr_sv_{SUFFIX}') "
                "ON CONFLICT (id) DO NOTHING"),
                {"l": f"orl_sv_{tag}_{SUFFIX}", "o": f"ord_sv_{tag}_{SUFFIX}",
                 "rl": f"rql_sv_{tag}_{SUFFIX}", "p": price})

    body = client.get("/v1/assistant/savings", headers=h).json()
    mine = [i for i in body["items"] if i["index_product_id"] == f"idx_sv_{SUFFIX}"]
    # 95 is the most recent price and it is *below* today's 100, so there is
    # no saving to report at all — the July line must not resurrect one
    assert mine == [], mine
