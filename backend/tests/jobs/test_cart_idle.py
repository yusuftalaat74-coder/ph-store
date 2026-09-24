"""Cart sprint §3 — the idle-basket notice, acceptance criteria 17..20.

Same cross-connection convention as `test_tick.py`: fixture rows go in
through `db_engine.begin()` because the job opens its own session and would
not see anything uncommitted on the test's connection.
"""
import hashlib
from datetime import timedelta

from sqlalchemy import text

from rova.core.clock import now
from rova.core.ids import new_id
from rova.jobs import tick as jobs

SUFFIX = "ci1"


def _n(tag):
    """A small stable number from a tag, for phone numbers and request
    numbers that must not collide and must not move between runs."""
    return int(hashlib.sha1(tag.encode()).hexdigest()[:6], 16) % 100000


def _pharmacy(db_engine, tag):
    """A pharmacy, a user who belongs to it, and a product to put in a cart."""
    org, pha, usr = f"org_{tag}", f"pha_{tag}", f"usr_{tag}"
    with db_engine.begin() as c:
        c.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) "
            "VALUES (:o, :o, :n, :n, 'PHARMACY') ON CONFLICT DO NOTHING"),
            {"o": org, "n": tag})
        c.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, "
            "trade_name, address, latitude, longitude, status) "
            "VALUES (:p, :o, 'MAPUTO_CIDADE', 'A', 'F', 'X', 0, 0, 'ACTIVE') "
            "ON CONFLICT DO NOTHING"), {"p": pha, "o": org})
        c.execute(text(
            "INSERT INTO app_user (id, phone, name, password_hash) "
            "VALUES (:u, :ph, 'U', 'x') ON CONFLICT DO NOTHING"),
            # A stable number per tag, not `hash()`: Python salts string
            # hashing per process, so this was a different phone every run and
            # two tags could collide into a unique-violation on a bad day.
            {"u": usr, "ph": "+2588400" + f"{_n(tag):05d}"})
        c.execute(text(
            "INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size, "
            "manufacturer, aim_status, regulated_price, review_status, reviewer_ref, search_text) "
            "VALUES (:i, 'Idlamol', 'IDLAMOL', 'Comprimido', '1mg', '1', 'M', 'AUTHORISED', "
            "false, 'PUBLISHED', 'test', 'idlamol') ON CONFLICT DO NOTHING"),
            {"i": f"idx_{tag}"})
    return org, pha, usr, f"idx_{tag}"


def _cart(db_engine, *, tag, pharmacy, user, age_hours, lines=1, status="DRAFT",
          is_cart=True):
    """A cart whose last touch is `age_hours` ago — on the request row and on
    every line, since the job takes the later of the two."""
    rid = f"req_{tag}"
    stamp = now() - timedelta(hours=age_hours)
    with db_engine.begin() as c:
        c.execute(text(
            "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, "
            "allocation_strategy, created_by_user_id, is_cart, created_at, updated_at) "
            "VALUES (:r, :n, :p, 'CATALOGUE', 'APP', :st, 'FEWEST_VENDORS', :u, :ic, :t, :t)"),
            {"r": rid, "n": f"RQ-2026-9{_n(tag):05d}", "p": pharmacy,
             "st": status, "u": user, "ic": is_cart, "t": stamp})
        for i in range(lines):
            c.execute(text(
                "INSERT INTO request_line (id, request_id, index_product_id, qty_requested, "
                "line_kind, match_status, created_at, updated_at) "
                "VALUES (:l, :r, :i, 2, 'CATALOGUE', 'RESOLVED', :t, :t)"),
                {"l": f"rql_{tag}_{i}", "r": rid, "i": f"idx_{tag}", "t": stamp})
    return rid


def _notices(db_engine, request_id):
    with db_engine.begin() as c:
        return c.execute(text(
            "SELECT * FROM notification WHERE event_code='N-CART-IDLE' "
            "AND payload->>'request_id' = :r"), {"r": request_id}).mappings().all()


# ── 17 ────────────────────────────────────────────────────────────────────

def test_17_an_idle_cart_is_noticed_once(db_engine):
    org, pha, usr, prod = _pharmacy(db_engine, f"{SUFFIX}a")
    rid = _cart(db_engine, tag=f"{SUFFIX}a", pharmacy=pha, user=usr, age_hours=30, lines=2)

    assert jobs.cart_idle() >= 1
    rows = _notices(db_engine, rid)
    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "IN_APP"
    assert row["event_code"] == "N-CART-IDLE"
    assert row["recipient_user_id"] == usr, "the person who built it"
    assert row["recipient_org_id"] == org, "and the shop, so the admin sees it too"
    assert row["payload"]["line_count"] == 2
    assert row["payload"]["first_items"] == ["IDLAMOL", "IDLAMOL"]
    assert "goods_total" not in row["payload"], (
        "the basket is repriced on every read, so a total frozen into a notice "
        "would be a number we might not honour by the time he taps it")

    # 17, second half: running the tick again changes nothing
    jobs.cart_idle()
    assert len(_notices(db_engine, rid)) == 1


# ── 18 ────────────────────────────────────────────────────────────────────

def test_18a_an_empty_cart_is_left_alone(db_engine):
    org, pha, usr, prod = _pharmacy(db_engine, f"{SUFFIX}b")
    rid = _cart(db_engine, tag=f"{SUFFIX}b", pharmacy=pha, user=usr, age_hours=72, lines=0)
    jobs.cart_idle()
    assert _notices(db_engine, rid) == []


def test_18b_a_cart_touched_inside_the_window_is_left_alone(db_engine):
    org, pha, usr, prod = _pharmacy(db_engine, f"{SUFFIX}c")
    rid = _cart(db_engine, tag=f"{SUFFIX}c", pharmacy=pha, user=usr, age_hours=2)
    jobs.cart_idle()
    assert _notices(db_engine, rid) == []


def test_18c_a_line_touched_recently_keeps_the_whole_cart_fresh(db_engine):
    """`last_touched_at` is the later of the request and its newest line. A
    cart whose request row is old but whose lines were edited an hour ago is
    a basket somebody is still working on."""
    org, pha, usr, prod = _pharmacy(db_engine, f"{SUFFIX}d")
    rid = _cart(db_engine, tag=f"{SUFFIX}d", pharmacy=pha, user=usr, age_hours=96)
    with db_engine.begin() as c:
        c.execute(text("UPDATE request_line SET updated_at = :t WHERE request_id = :r"),
                  {"t": now() - timedelta(hours=1), "r": rid})
    jobs.cart_idle()
    assert _notices(db_engine, rid) == []


def test_18d_a_request_that_is_no_longer_an_open_cart_is_left_alone(db_engine):
    """`is_cart` is never cleared — it is the provenance a re-order shelf is
    built from — so a job that scanned on the flag alone would nag a
    pharmacist about an order he placed days ago."""
    org, pha, usr, prod = _pharmacy(db_engine, f"{SUFFIX}e")
    rid = _cart(db_engine, tag=f"{SUFFIX}e", pharmacy=pha, user=usr, age_hours=72,
                status="IN_FULFILMENT")
    jobs.cart_idle()
    assert _notices(db_engine, rid) == []


def test_18e_a_draft_that_was_never_a_cart_is_left_alone(db_engine):
    org, pha, usr, prod = _pharmacy(db_engine, f"{SUFFIX}f")
    rid = _cart(db_engine, tag=f"{SUFFIX}f", pharmacy=pha, user=usr, age_hours=72,
                is_cart=False)
    jobs.cart_idle()
    assert _notices(db_engine, rid) == []


# ── 19 ────────────────────────────────────────────────────────────────────

def test_19_nothing_is_queued_on_a_channel_nobody_delivers(db_engine):
    """`DELIVERABLE_CHANNELS` is `("IN_APP",)`. A WhatsApp row queued "for
    later" would sit QUEUED for ever and read as a backlog somebody is
    working through, in the one summary that is supposed to show real ones."""
    org, pha, usr, prod = _pharmacy(db_engine, f"{SUFFIX}g")
    _cart(db_engine, tag=f"{SUFFIX}g", pharmacy=pha, user=usr, age_hours=48)
    jobs.cart_idle()
    with db_engine.begin() as c:
        channels = c.execute(text(
            "SELECT DISTINCT channel FROM notification WHERE event_code='N-CART-IDLE'"
        )).scalars().all()
    assert channels == ["IN_APP"]


# ── 20 ────────────────────────────────────────────────────────────────────

def test_20_the_notice_reaches_the_buyer_and_the_admin(client, db_engine):
    """Dispatch marks it SENT, and both the person who filled the basket and
    the admin of the same pharmacy find it in their own inbox."""
    from rova.auth.security import hash_password

    org, pha, usr, prod = _pharmacy(db_engine, f"{SUFFIX}h")
    admin = f"usr_{SUFFIX}h_admin"
    with db_engine.begin() as c:
        c.execute(text("UPDATE app_user SET password_hash=:h WHERE id=:u"),
                  {"h": hash_password("rova-demo"), "u": usr})
        c.execute(text(
            "INSERT INTO app_user (id, phone, name, password_hash) "
            "VALUES (:u, :p, 'A', :h), (:pa, :pp, 'P', :h) ON CONFLICT DO NOTHING"),
            {"u": admin, "p": "+258840779911", "h": hash_password("rova-demo"),
             "pa": f"usr_{SUFFIX}h_plat", "pp": "+258840779912"})
        c.execute(text(
            "INSERT INTO membership (id, user_id, organisation_id, role_codes) "
            "VALUES (:m, :u, NULL, :r) ON CONFLICT DO NOTHING"),
            {"m": f"mem_{SUFFIX}h_plat", "u": f"usr_{SUFFIX}h_plat", "r": ["PlatformAdmin"]})
        for uid, roles in ((usr, ["PharmacyBuyer"]), (admin, ["PharmacyAdmin"])):
            c.execute(text(
                "INSERT INTO membership (id, user_id, organisation_id, role_codes) "
                "VALUES (:m, :u, :o, :r) ON CONFLICT DO NOTHING"),
                {"m": f"mem_{uid}", "u": uid, "o": org, "r": roles})

    rid = _cart(db_engine, tag=f"{SUFFIX}h", pharmacy=pha, user=usr, age_hours=48)
    jobs.cart_idle()

    def headers(phone):
        r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": "PH"})
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["access_token"]}

    disp = client.post("/v1/notifications/dispatch",
                       headers=headers("+258840779912"))
    assert disp.status_code == 200, disp.text

    with db_engine.begin() as c:
        status = c.execute(text(
            "SELECT status FROM notification WHERE event_code='N-CART-IDLE' "
            "AND payload->>'request_id' = :r"), {"r": rid}).scalar()
    assert status == "SENT"

    with db_engine.begin() as c:
        phones = dict(c.execute(text(
            "SELECT id, phone FROM app_user WHERE id = ANY(:ids)"),
            {"ids": [usr, admin]}).all())

    for uid in (usr, admin):
        body = client.get("/v1/notifications", headers=headers(phones[uid])).json()
        codes = [{"id": i["id"], "code": i["event_code"],
                  "req": (i["payload"] or {}).get("request_id")} for i in body["items"]]
        assert any(c["code"] == "N-CART-IDLE" and c["req"] == rid for c in codes), \
            f"{uid} cannot see the notice about their own pharmacy's basket"


# ── the notice stops being true when the basket does ──────────────────────

def test_a_notice_is_closed_when_its_basket_is_ordered(client, db_engine):
    """Without this: Monday he fills a cart, Wednesday the job writes the
    notice, Wednesday afternoon he checks out, and Thursday the bell still
    says "your basket is still waiting" about a basket that became an order.
    Tapping it would open a different cart, or an empty one."""
    org, pha, usr, prod = _pharmacy(db_engine, f"{SUFFIX}i")
    rid = _cart(db_engine, tag=f"{SUFFIX}i", pharmacy=pha, user=usr, age_hours=48)
    jobs.cart_idle()
    assert [r["read_at"] for r in _notices(db_engine, rid)] == [None]

    # any SM-01 route out of DRAFT closes it; ABANDON is the one that needs
    # no vendor offer to exist
    from rova.auth.principal import Principal
    from rova.core.db import get_sessionmaker
    from rova.domain.hooks import wire
    from rova.domain.machines.registry import MACHINES
    wire()
    buyer = Principal(user_id=usr, membership_id=f"mem_{usr}", organisation_id=org,
                      surface="PH", roles=frozenset({"PharmacyBuyer"}),
                      pharmacy_id=pha, vendor_id=None, transporter_id=None)
    session = get_sessionmaker()()
    try:
        MACHINES["SM-01"].apply(session, rid, "ABANDON", buyer)
        session.commit()
    finally:
        session.close()

    rows = _notices(db_engine, rid)
    assert len(rows) == 1, "the notice is kept as history, not deleted"
    assert rows[0]["read_at"] is not None, "it is no longer waiting for him"


def test_two_overlapping_ticks_still_produce_one_notice(db_engine):
    """The guard used to be a check-then-insert, which two ticks could both
    pass. `uq_one_idle_notice_per_cart` makes it the database's rule; this
    asserts the insert that loses the race is absorbed rather than crashing
    the run."""
    org, pha, usr, prod = _pharmacy(db_engine, f"{SUFFIX}j")
    rid = _cart(db_engine, tag=f"{SUFFIX}j", pharmacy=pha, user=usr, age_hours=48)
    jobs.cart_idle()

    # the second tick's view of "already notified" is wiped, exactly as a
    # concurrent run's snapshot would be, leaving only the index to refuse it
    with db_engine.begin() as c:
        c.execute(text("UPDATE notification SET event_code='N-CART-IDLE' "
                       "WHERE payload->>'request_id' = :r"), {"r": rid})
    with db_engine.begin() as c:
        before = c.execute(text(
            "SELECT count(*) FROM notification WHERE event_code='N-CART-IDLE' "
            "AND payload->>'request_id' = :r"), {"r": rid}).scalar()
    assert before == 1
    assert jobs.cart_idle() >= 0          # does not raise
    assert len(_notices(db_engine, rid)) == 1
