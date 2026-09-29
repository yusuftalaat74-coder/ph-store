"""Signup spec §5.1 — a pharmacy signs itself up from the phone, browses and
fills a cart while it waits, and a reviewer approves or rejects it.

Each test owns its phones (unique per test) and sends its own
`X-Forwarded-For`, because the per-IP limit is real and the whole module runs
from one test client."""
import itertools
import os
import time

import pytest
from sqlalchemy import text

from .test_onboarding import _platform_user

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5f0000000049454e44ae426082")

_seq = itertools.count(1)
_RUN = f"{os.getpid() % 1000:03d}{int(time.time()) % 1000:03d}"


def _phone() -> str:
    """A fresh +25884 7xxxxxx number: unique per call, per run."""
    n = next(_seq)
    return f"+258847{_RUN[-3:]}{n:03d}"


def _ip() -> str:
    n = next(_seq)
    return f"198.51.{n // 250}.{n % 250 + 1}"


def _form(phone: str, **over) -> dict:
    data = {"pharmacy_name": "Farmácia Nova Esperança", "owner_name": "Ana Muianga", "phone": phone,
            "password": "segredo-123", "city": "MAPUTO", "neighbourhood": "Polana Caniço",
            "locale": "pt", "accept_terms": "true"}
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def _signup(client, phone=None, *, file=PNG_1PX, ip=None, **over):
    files = {"licence_file": ("alvara.png", file, "image/png")} if file is not None else None
    return client.post("/v1/auth/signup", data=_form(phone or _phone(), **over), files=files,
                       headers={"X-Forwarded-For": ip or _ip()})


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _login(client, phone, surface="OP"):
    r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": surface})
    assert r.status_code == 200, r.text
    return _h(r.json()["access_token"])


def _reviewer(client, db_engine, suffix, role):
    return _login(client, _platform_user(db_engine, suffix, role))


def _one(db_engine, sql, **params):
    with db_engine.connect() as c:
        return c.execute(text(sql), params).mappings().first()


def _approve(client, h, pharmacy_id, **over):
    body = {"licence_number": "RET-2026-0123", "issue_date": "2026-01-01", "expiry_date": "2099-01-01"}
    body.update(over)
    return client.post(f"/v1/onboarding-review/pharmacies/{pharmacy_id}/approve", headers=h, json=body)


def _priced_product(db_engine, pharmacy_id, tag):
    """A vendor with one published, priced offer and a credit line for this
    pharmacy — the minimum `checkout()` needs to make an order."""
    with db_engine.begin() as c:
        c.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            "(:o, :t, 'Vendor SU', 'vendor su', 'VENDOR')"), {"o": f"org_su_{tag}", "t": f"SU{tag}"})
        c.execute(text(
            "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
            "delivery_mode, mov_amount, acceptance_mode, status) VALUES (:v, :o, 'MAPUTO_CIDADE', "
            "'Vendor SU', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE')"),
            {"v": f"ven_su_{tag}", "o": f"org_su_{tag}"})
        c.execute(text(
            "INSERT INTO index_product (id, inn, brand_name, form, strength, pack_size, manufacturer, category, "
            "aim_status, regulated_price, review_status, reviewer_ref, search_text) VALUES (:p, 'Signupinol', "
            ":b, 'Comprimido', '500mg', '20', 'M', 'Comprimidos', 'AUTHORISED', false, 'PUBLISHED', 'test', :s)"),
            {"p": f"idx_su_{tag}", "b": f"SIGNUPINOL {tag}", "s": f"signupinol {tag}"})
        c.execute(text(
            "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
            "pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES (:id, :v, :p, false, 500, '1', "
            "400, 50.00, now())"), {"id": f"ofr_su_{tag}", "v": f"ven_su_{tag}", "p": f"idx_su_{tag}"})
        c.execute(text(
            "INSERT INTO credit_facility (id, vendor_id, pharmacy_id, limit_amount, opening_balance, terms_days, "
            "status) VALUES (:id, :v, :ph, 100000.00, 0, 30, 'ACTIVE')"),
            {"id": f"crf_su_{tag}", "v": f"ven_su_{tag}", "ph": pharmacy_id})
    return f"idx_su_{tag}"


# ------------------------------------------------------------------ 1-2

def test_signup_creates_everything_and_logs_in(client, db_engine):
    phone = _phone()
    r = _signup(client, phone)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pharmacy"]["status"] == "ONBOARDING"
    assert body["licence"] == {"id": body["licence"]["id"], "status": "SUBMITTED", "has_document": True}
    assert body["selected_membership_id"] == body["memberships"][0]["id"]

    me = client.get("/v1/auth/me", headers=_h(body["access_token"])).json()
    assert me["roles"] == ["PharmacyAdmin", "PharmacyBuyer", "PharmacyReceiver"]
    assert me["pharmacy_id"] == body["pharmacy"]["id"]
    assert me["pharmacy_status"] == "ONBOARDING"

    pid, lid = body["pharmacy"]["id"], body["licence"]["id"]
    ph = _one(db_engine, "SELECT * FROM pharmacy_account WHERE id=:p", p=pid)
    assert ph["status"] == "ONBOARDING" and ph["region_code"] == "MAPUTO_CIDADE"
    assert ph["address"] == "Polana Caniço, Maputo" and ph["latitude"] is None
    org = _one(db_engine, "SELECT * FROM organisation WHERE id=:o", o=ph["organisation_id"])
    assert org["tax_id"] is None and org["legal_name_normalised"] == "farmácia nova esperança"
    lic = _one(db_engine, "SELECT * FROM licence WHERE id=:l", l=lid)
    assert lic["status"] == "SUBMITTED" and lic["document_ref"].startswith(f"licences/{pid}/")
    assert lic["number"] is None and lic["expiry_date"] is None
    user = _one(db_engine, "SELECT * FROM app_user WHERE phone=:p", p=phone)
    assert user["name"] == "Ana Muianga" and user["password_hash"].startswith("$argon2")
    mem = _one(db_engine, "SELECT * FROM membership WHERE user_id=:u", u=user["id"])
    assert mem["status"] == "ACTIVE" and mem["organisation_id"] == org["id"]
    case = _one(db_engine, "SELECT * FROM verification_case WHERE organisation_id=:o", o=org["id"])
    assert case["decision"] == "PENDING" and case["licence_id"] == lid
    assert case["subject_type"] == "PHARMACY_ONBOARDING"
    with db_engine.connect() as c:
        roles = sorted(c.execute(text(
            "SELECT recipient_role FROM notification WHERE event_code='N-PHARMACY-SIGNUP' "
            "AND payload->>'pharmacy_id'=:p"), {"p": pid}).scalars())
        outcome = c.execute(text("SELECT outcome FROM signup_attempt WHERE phone=:p"), {"p": phone}).scalars().all()
        audit = c.execute(text("SELECT action_code, actor_user_id FROM audit_event WHERE subject_id=:o"),
                          {"o": org["id"]}).mappings().all()
    assert roles == ["ComplianceOfficer", "OpsReviewer", "PlatformAdmin"]
    assert outcome == ["CREATED"]
    assert [(a["action_code"], a["actor_user_id"]) for a in audit] == [("ROLE_MEMBERSHIP_CHANGE", user["id"])]


def test_signup_without_file_is_allowed(client, db_engine):
    r = _signup(client, file=None, nuit="400123456", city="MATOLA", neighbourhood="")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["licence"]["has_document"] is False
    lic = _one(db_engine, "SELECT document_ref FROM licence WHERE id=:l", l=body["licence"]["id"])
    assert lic["document_ref"] is None
    st = client.get("/v1/pharmacies/me/review-status", headers=_h(body["access_token"]))
    assert st.status_code == 200, st.text
    st = st.json()
    assert st["pharmacy_status"] == "ONBOARDING" and st["can_checkout"] is False
    assert st["licence"]["has_document"] is False and st["rejection_reason"] is None
    ph = _one(db_engine, "SELECT p.address, p.region_code, o.tax_id FROM pharmacy_account p "
                         "JOIN organisation o ON o.id=p.organisation_id WHERE p.id=:p", p=body["pharmacy"]["id"])
    assert (ph["address"], ph["region_code"], ph["tax_id"]) == ("Matola", "MATOLA", "400123456")


# ------------------------------------------------------------------ 3-6

def test_duplicate_phone_is_409_with_its_own_code(client, db_engine):
    phone = _phone()                                     # +258847…
    assert _signup(client, phone).status_code == 201
    local = f"{phone[4:6]} {phone[6:9]} {phone[9:]}"     # "84 7xx xxxx"
    r = _signup(client, local)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "PHONE_ALREADY_REGISTERED"
    with db_engine.connect() as c:
        outcomes = c.execute(text("SELECT outcome FROM signup_attempt WHERE phone=:p ORDER BY created_at"),
                             {"p": phone}).scalars().all()
    assert outcomes == ["CREATED", "DUPLICATE_PHONE"]


@pytest.mark.parametrize("over, field", [
    ({"phone": "71234567"}, "phone"),
    ({"password": "short"}, "password"),
    ({"accept_terms": None}, "accept_terms"),
    ({"nuit": "12345"}, "nuit"),
    ({"city": "BEIRA"}, "city"),
    ({"pharmacy_name": " x "}, "pharmacy_name"),
])
def test_invalid_fields_are_422_naming_the_field(client, db_engine, over, field):
    phone = over.pop("phone", None) or _phone()
    r = _signup(client, phone, **over)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"][0]["field"] == field
    with db_engine.connect() as c:
        n = c.execute(text("SELECT count(*) FROM app_user WHERE phone=:p"), {"p": phone}).scalar()
    assert n == 0, "a refused sign-up must not leave a user behind"


def test_bad_file_type_is_422(client, db_engine):
    phone = _phone()
    r = client.post("/v1/auth/signup", data=_form(phone),
                    files={"licence_file": ("alvara.jpg", b"this is not a photo at all", "image/jpeg")},
                    headers={"X-Forwarded-For": _ip()})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"][0] == {"field": "licence_file", "reason": "unsupported_type"}
    r = client.post("/v1/auth/signup", data=_form(phone),
                    files={"licence_file": ("alvara.png", b"", "image/png")}, headers={"X-Forwarded-For": _ip()})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["reason"] == "empty"
    with db_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM app_user WHERE phone=:p"), {"p": phone}).scalar() == 0


@pytest.fixture
def phone_limit_two(db_engine):
    with db_engine.begin() as c:
        c.execute(text("UPDATE config_parameter SET value='2' WHERE key='CFG-SIGNUP-MAX-PER-PHONE-PER-DAY' "
                       "AND scope_type='GLOBAL'"))
    yield
    with db_engine.begin() as c:
        c.execute(text("UPDATE config_parameter SET value='3' WHERE key='CFG-SIGNUP-MAX-PER-PHONE-PER-DAY' "
                       "AND scope_type='GLOBAL'"))


def test_signup_is_rate_limited_per_phone(client, db_engine, phone_limit_two):
    phone = _phone()
    assert _signup(client, phone).status_code == 201
    assert _signup(client, phone).status_code == 409
    r = _signup(client, phone)
    assert r.status_code == 429, r.text
    err = r.json()["error"]
    assert err["code"] == "RATE_LIMITED"
    assert err["details"] == [{"field": "retry_after_seconds", "reason": "3600"}]
    with db_engine.connect() as c:
        assert c.execute(text("SELECT outcome FROM signup_attempt WHERE phone=:p ORDER BY created_at DESC "
                              "LIMIT 1"), {"p": phone}).scalar() == "RATE_LIMITED"


def test_signup_is_rate_limited_per_ip_even_for_garbage(client):
    """Ten attempts from one address in an hour, whatever they were — even
    numbers that do not parse — and the eleventh is refused."""
    ip = _ip()
    for _ in range(10):
        assert _signup(client, "123", ip=ip).status_code == 422
    r = _signup(client, ip=ip)
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"


def test_forwarded_for_is_not_trusted_from_a_public_peer():
    from starlette.requests import Request
    from rova.onboarding.signup_router import _client_ip

    def req(peer, xff):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": headers, "client": (peer, 1234)})

    assert _client_ip(req("41.220.1.2", "1.2.3.4")) == "41.220.1.2"          # direct: header ignored
    assert _client_ip(req("172.18.0.5", "1.2.3.4, 41.220.1.2")) == "41.220.1.2"  # via our proxy: its hop
    assert _client_ip(req("172.18.0.5", None)) == "172.18.0.5"


# ------------------------------------------------------------------ 7-9

def test_onboarding_pharmacy_can_browse_and_cart_but_not_checkout(client, db_engine):
    body = _signup(client).json()
    h = _h(body["access_token"])
    product = _priced_product(db_engine, body["pharmacy"]["id"], "b7")
    assert client.get("/v1/catalogue/search", headers=h, params={"q": "signupinol"}).status_code == 200
    r = client.post("/v1/cart/lines", headers=h, json={"lines": [{"index_product_id": product, "qty_requested": 2}]})
    assert r.status_code == 200, r.text
    cart = r.json()
    r = client.post(f"/v1/requests/{cart['request_id']}/checkout", headers={**h, "Idempotency-Key": "su-b7"},
                    json={})
    assert r.status_code == 403, r.text
    err = r.json()["error"]
    assert err["code"] == "PHARMACY_NOT_ACTIVE"
    assert err["details"] == [{"field": "pharmacy_status", "reason": "ONBOARDING"}]
    again = client.get("/v1/cart", headers=h).json()
    assert again["request_id"] == cart["request_id"] and len(again["lines"]) == 1, "the cart is kept"


def test_reviewer_list_shows_the_new_pharmacy(client, db_engine):
    phone = _phone()
    body = _signup(client, phone, file=None).json()
    pid = body["pharmacy"]["id"]
    for i, role in enumerate(("admin", "ops", "compliance")):
        h = _reviewer(client, db_engine, f"s8{i}", role)
        r = client.get("/v1/onboarding-review/pharmacies", headers=h)
        assert r.status_code == 200, r.text
        item = next(x for x in r.json()["items"] if x["pharmacy_id"] == pid)
        assert item["owner"]["phone"] == phone and item["owner"]["name"] == "Ana Muianga"
        assert item["licence"]["has_document"] is False and item["licence"]["type"] == "RETAIL_A"
        assert item["status"] == "ONBOARDING" and item["case_id"].startswith("vcs_")
        assert item["same_name_pharmacies"] >= 1, "every test signs up the same name"
    # a pharmacy account cannot read the queue
    assert client.get("/v1/onboarding-review/pharmacies", headers=_h(body["access_token"])).status_code == 403


def _transitions(db_engine, subject_id):
    with db_engine.connect() as c:
        return [tuple(r) for r in c.execute(text(
            "SELECT machine, trigger FROM state_transition WHERE subject_id=:s ORDER BY occurred_at, id"),
            {"s": subject_id}).all()]


def test_approve_requires_document(client, db_engine):
    body = _signup(client, file=None).json()
    pid, lid = body["pharmacy"]["id"], body["licence"]["id"]
    ph_h = _h(body["access_token"])
    product = _priced_product(db_engine, pid, "b9")
    cart = client.post("/v1/cart/lines", headers=ph_h,
                       json={"lines": [{"index_product_id": product, "qty_requested": 1}]}).json()
    h = _reviewer(client, db_engine, "s90", "admin")

    r = _approve(client, h, pid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "GUARD_FAILED", r.text

    r = client.post(f"/v1/onboarding-review/pharmacies/{pid}/licence-document", headers=h,
                    files={"licence_file": ("a.png", PNG_1PX, "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["licence"]["has_document"] is True
    r = client.post(f"/v1/onboarding-review/pharmacies/{pid}/licence-document", headers=h,
                    files={"licence_file": ("a.png", PNG_1PX, "image/png")})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONFLICT", "replace is never silent"

    assert _approve(client, h, pid, expiry_date="2025-12-31").status_code == 422
    assert _approve(client, h, pid, issue_date="2099-02-01").status_code == 422

    r = _approve(client, h, pid, licence_type="B", notes="liguei, confirmou")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ACTIVE"
    assert r.json()["licence"]["status"] == "VALID" and r.json()["licence"]["type"] == "RETAIL_B"

    lic = _one(db_engine, "SELECT * FROM licence WHERE id=:l", l=lid)
    assert (lic["number"], str(lic["issue_date"]), str(lic["expiry_date"])) == \
        ("RET-2026-0123", "2026-01-01", "2099-01-01")
    assert _one(db_engine, "SELECT licence_type FROM pharmacy_account WHERE id=:p", p=pid)["licence_type"] == "B"
    case = _one(db_engine, "SELECT * FROM verification_case WHERE licence_id=:l", l=lid)
    assert case["decision"] == "APPROVED" and case["decision_notes"] == "liguei, confirmou"
    assert case["reviewer_user_id"] == "usr_onb_s90"
    assert _transitions(db_engine, lid) == [("SM-08", "OPEN_REVIEW"), ("SM-08", "APPROVE")]
    assert _transitions(db_engine, pid) == [("SM-07", "APPROVE")]
    org = _one(db_engine, "SELECT organisation_id FROM pharmacy_account WHERE id=:p", p=pid)["organisation_id"]
    assert _one(db_engine, "SELECT 1 AS x FROM notification WHERE event_code='N-PHARMACY-APPROVED' "
                           "AND recipient_org_id=:o", o=org)
    assert _one(db_engine, "SELECT metadata FROM audit_event WHERE subject_id=:p AND "
                           "action_code='LICENCE_DECISION'", p=pid)["metadata"]["decision"] == "APPROVED"

    st = client.get("/v1/pharmacies/me/review-status", headers=ph_h).json()
    assert st["can_checkout"] is True and st["pharmacy_status"] == "ACTIVE"
    r = client.post(f"/v1/requests/{cart['request_id']}/checkout",
                    headers={**ph_h, "Idempotency-Key": "su-b9"}, json={})
    assert r.status_code == 200, r.text
    assert r.json()["orders"], r.text

    assert _approve(client, h, pid).status_code == 409, "an ACTIVE pharmacy is not approved twice"


# ------------------------------------------------------------------ 10-13

def test_each_reviewer_role_can_approve(client, db_engine):
    for i, role in enumerate(("admin", "ops", "compliance")):
        pid = _signup(client).json()["pharmacy"]["id"]
        h = _reviewer(client, db_engine, f"s10{i}", role)
        r = _approve(client, h, pid)
        assert r.status_code == 200, (role, r.text)
        assert r.json()["status"] == "ACTIVE"
    other = _signup(client).json()
    pid = _signup(client).json()["pharmacy"]["id"]
    r = _approve(client, _h(other["access_token"]), pid)
    assert r.status_code == 403


def test_reject_then_reupload_then_approve(client, db_engine):
    body = _signup(client).json()
    pid, first_lic = body["pharmacy"]["id"], body["licence"]["id"]
    ph_h = _h(body["access_token"])
    h = _reviewer(client, db_engine, "s11", "compliance")

    assert client.post(f"/v1/onboarding-review/pharmacies/{pid}/reject", headers=h,
                       json={"reason": "  "}).status_code == 422
    r = client.post(f"/v1/onboarding-review/pharmacies/{pid}/reject", headers=h,
                    json={"reason": "foto ilegível"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REJECTED"
    assert _one(db_engine, "SELECT status FROM licence WHERE id=:l", l=first_lic)["status"] == "REJECTED"
    st = client.get("/v1/pharmacies/me/review-status", headers=ph_h).json()
    assert st["pharmacy_status"] == "REJECTED" and st["rejection_reason"] == "foto ilegível"
    assert st["decided_at"] is not None
    org = _one(db_engine, "SELECT organisation_id FROM pharmacy_account WHERE id=:p", p=pid)["organisation_id"]
    n = _one(db_engine, "SELECT payload FROM notification WHERE event_code='N-PHARMACY-REJECTED' "
                        "AND recipient_org_id=:o", o=org)
    assert n["payload"]["reason"] == "foto ilegível"

    r = client.post("/v1/pharmacies/me/licence", headers=ph_h,
                    files={"licence_file": ("nova.png", PNG_1PX, "image/png")})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["pharmacy_status"] == "ONBOARDING" and st["licence"]["status"] == "SUBMITTED"
    assert st["licence"]["id"] != first_lic and st["licence"]["has_document"] is True
    resubmit = _one(db_engine, "SELECT actor_user_id, actor_role FROM state_transition WHERE subject_id=:p "
                               "AND trigger='RESUBMIT'", p=pid)
    user_id = _one(db_engine, "SELECT user_id FROM membership WHERE organisation_id=:o", o=org)["user_id"]
    assert resubmit["actor_user_id"] == user_id
    with db_engine.connect() as c:
        cases = c.execute(text("SELECT decision FROM verification_case WHERE organisation_id=:o "
                               "ORDER BY opened_at"), {"o": org}).scalars().all()
    assert cases == ["REJECTED", "PENDING"]

    r = _approve(client, h, pid)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ACTIVE" and r.json()["licence"]["id"] == st["licence"]["id"]
    # an approved pharmacy renews through compliance, not through this button
    r = client.post("/v1/pharmacies/me/licence", headers=ph_h,
                    files={"licence_file": ("x.png", PNG_1PX, "image/png")})
    assert r.status_code == 409 and r.json()["error"]["code"] == "GUARD_FAILED"


def test_first_upload_from_the_browser_fills_the_waiting_licence(client, db_engine):
    body = _signup(client, file=None).json()
    r = client.post("/v1/pharmacies/me/licence", headers=_h(body["access_token"]),
                    files={"licence_file": ("a.pdf", b"%PDF-1.4\n%fake but typed\n", "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["licence"]["id"] == body["licence"]["id"], "no new row: the waiting licence gets the file"
    assert r.json()["licence"]["has_document"] is True


def test_document_is_served_only_to_the_right_people(client, db_engine):
    body = _signup(client).json()
    lid = body["licence"]["id"]
    url = f"/v1/licences/{lid}/document"
    r = client.get(url, headers=_h(body["access_token"]))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png" and r.content == PNG_1PX
    assert r.headers["content-disposition"] == "inline"

    stranger = _signup(client).json()
    assert client.get(url, headers=_h(stranger["access_token"])).status_code == 404
    assert client.get(url, headers=_reviewer(client, db_engine, "s12", "ops")).status_code == 200
    assert client.get(url).status_code == 401

    no_doc = _signup(client, file=None).json()
    r = client.get(f"/v1/licences/{no_doc['licence']['id']}/document", headers=_h(no_doc["access_token"]))
    assert r.status_code == 404


def test_existing_login_and_me_are_unchanged(client, db_engine):
    from .test_auth import _setup_user
    _setup_user(db_engine, "s13")
    r = client.post("/v1/auth/login", json={"phone": "+2588400008s13", "password": "rova-demo", "surface": "PH"})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"access_token", "refresh_token", "memberships", "selected_membership_id"}
    me = client.get("/v1/auth/me", headers=_h(r.json()["access_token"])).json()
    assert me["roles"] == ["PharmacyBuyer"]
    assert "pharmacy_status" in me and me["pharmacy_status"] == "ONBOARDING"
    platform = client.get("/v1/auth/me", headers=_reviewer(client, db_engine, "s13", "admin")).json()
    assert platform["pharmacy_id"] is None and platform["pharmacy_status"] is None
