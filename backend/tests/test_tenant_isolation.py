"""A2.6, A17 'Tenant isolation' row / B3.24 — a foreign pharmacy principal
gets 404, never 403 or 200, on another pharmacy's resources."""
from sqlalchemy import text

from rova.auth.security import hash_password


def _pharmacy(db_engine, suffix: str):
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"('org_ti_{suffix}', '97{suffix}', 'P', 'p', 'PHARMACY')"
        ))
        conn.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, "
            "address, latitude, longitude) VALUES "
            f"('pha_ti_{suffix}', 'org_ti_{suffix}', 'MAPUTO_CIDADE', 'A', 'P', 'X', 0, 0)"
        ))
        conn.execute(text(
            f"INSERT INTO app_user (id, phone, name, password_hash) VALUES "
            f"('usr_ti_{suffix}', :phone, 'U', :h)"
        ), {"phone": f"+2588400007{suffix}", "h": hash_password("rova-demo")})
        conn.execute(text(
            f"INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES "
            f"('mem_ti_{suffix}', 'usr_ti_{suffix}', 'org_ti_{suffix}', ARRAY['PharmacyBuyer'])"
        ))
        conn.execute(text(
            "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy) VALUES "
            f"('req_ti_{suffix}', :num, 'pha_ti_{suffix}', 'CATALOGUE', 'APP', 'DRAFT', 'FEWEST_VENDORS')"
        ), {"num": f"RQ-TI-{suffix}"})


def test_foreign_pharmacy_gets_404_not_403_or_200(client, db_engine):
    _pharmacy(db_engine, "c1")
    _pharmacy(db_engine, "c2")
    r = client.post("/v1/auth/login", json={"phone": "+2588400007c1", "password": "rova-demo", "surface": "PH"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    own = client.get("/v1/requests/req_ti_c1", headers=headers)
    assert own.status_code == 200

    foreign = client.get("/v1/requests/req_ti_c2", headers=headers)
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "NOT_FOUND"
