"""A17 'Auth & RBAC' row (reduced set) / B3.23 — membership revocation
invalidates an access token on the very next request, not only at refresh."""
from sqlalchemy import text

from rova.auth.security import hash_password


def _setup_user(db_engine, suffix: str):
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"('org_auth_{suffix}', '96{suffix}', 'P', 'p', 'PHARMACY')"
        ))
        conn.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, "
            "address, latitude, longitude) VALUES "
            f"('pha_auth_{suffix}', 'org_auth_{suffix}', 'MAPUTO_CIDADE', 'A', 'P', 'X', 0, 0)"
        ))
        conn.execute(text(
            f"INSERT INTO app_user (id, phone, name, password_hash) VALUES "
            f"('usr_auth_{suffix}', :phone, 'U', :h)"
        ), {"phone": f"+2588400008{suffix}", "h": hash_password("rova-demo")})
        conn.execute(text(
            f"INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES "
            f"('mem_auth_{suffix}', 'usr_auth_{suffix}', 'org_auth_{suffix}', ARRAY['PharmacyBuyer'])"
        ))


def test_login_returns_tokens_and_me_reflects_principal(client, db_engine):
    _setup_user(db_engine, "b1")
    r = client.post("/v1/auth/login", json={"phone": "+2588400008b1", "password": "rova-demo", "surface": "PH"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["roles"] == ["PharmacyBuyer"]


def test_wrong_password_rejected(client, db_engine):
    _setup_user(db_engine, "b2")
    r = client.post("/v1/auth/login", json={"phone": "+2588400008b2", "password": "wrong", "surface": "PH"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


def test_login_lockout_engages_after_configured_attempts_and_blocks_correct_password(client, db_engine):
    """B fix 1a: `get_session()` used to roll back the whole request
    transaction on the 401 it was about to raise, silently discarding the
    very `failed_login_count` UPDATE that would have engaged the lock — so
    seven wrong passwords, then the eighth (correct) one, logged straight
    in. This sends eight real HTTP requests against a real DB and asserts
    the lock actually engages: CFG-LOGIN-LOCKOUT-ATTEMPTS is seeded 5 (A-153),
    so the 5th wrong password must already report the account locked, and
    the correct password on a 6th attempt must still be refused — not just
    that `failed_login_count` incremented in the DB (which the old sham
    scenario could pass by accident once the counter itself worked, without
    proving the *lock* — the actual product behaviour — ever engages)."""
    _setup_user(db_engine, "b4")
    phone = "+2588400008b4"

    for attempt in range(1, 5):
        r = client.post("/v1/auth/login", json={"phone": phone, "password": "wrong", "surface": "PH"})
        assert r.status_code == 401, f"attempt {attempt}"
        assert r.json()["error"]["code"] == "UNAUTHENTICATED"

    with db_engine.connect() as conn:
        count = conn.execute(
            text("SELECT failed_login_count FROM app_user WHERE id='usr_auth_b4'")
        ).scalar()
    assert count == 4, "failed_login_count must survive the 401s, not be rolled back with them"

    # 5th wrong attempt: CFG-LOGIN-LOCKOUT-ATTEMPTS (5) reached, must lock.
    r5 = client.post("/v1/auth/login", json={"phone": phone, "password": "wrong", "surface": "PH"})
    assert r5.status_code == 401

    with db_engine.connect() as conn:
        row = conn.execute(
            text("SELECT failed_login_count, locked_until FROM app_user WHERE id='usr_auth_b4'")
        ).mappings().one()
    assert row["failed_login_count"] == 5
    assert row["locked_until"] is not None

    audit_count = None
    with db_engine.connect() as conn:
        audit_count = conn.execute(
            text("SELECT count(*) FROM audit_event WHERE action_code='LOGIN_LOCKOUT' AND subject_id='usr_auth_b4'")
        ).scalar()
    assert audit_count == 1

    # the CORRECT password, on the very next (6th, 7th, 8th...) attempt,
    # must still be refused while locked — this is the exact scenario the
    # review proved broken live ("seven wrong passwords then the eighth
    # correct one: logs in fine").
    for attempt in range(6, 9):
        r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": "PH"})
        assert r.status_code == 401, f"attempt {attempt}: correct password must still be locked out"
        assert r.json()["error"]["message"] == "account temporarily locked"


def test_missing_token_rejected():
    from fastapi.testclient import TestClient
    from rova.main import create_app
    with TestClient(create_app()) as c:
        r = c.get("/v1/auth/me")
        assert r.status_code == 401


def test_membership_revocation_invalidates_token_immediately(client, db_engine):
    _setup_user(db_engine, "b3")
    r = client.post("/v1/auth/login", json={"phone": "+2588400008b3", "password": "rova-demo", "surface": "PH"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/auth/me", headers=headers).status_code == 200

    with db_engine.begin() as conn:
        conn.execute(text("UPDATE membership SET status='REVOKED' WHERE id='mem_auth_b3'"))

    r2 = client.get("/v1/auth/me", headers=headers)
    assert r2.status_code == 401  # B3.23: fails on the very next request, not only at refresh
