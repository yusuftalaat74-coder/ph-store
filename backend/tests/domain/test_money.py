"""A17 'Money' row — no float in money paths, JSON as strings, ROUND_HALF_UP once."""
import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from rova.auth.security import hash_password
from rova.core.money import money_str, quantize

SRC = Path(__file__).resolve().parents[2] / "src" / "rova"
DDL = Path(__file__).resolve().parents[2] / "migrations" / "sql" / "0001_initial.sql"


def test_no_float_applied_to_money_field_names():
    money_field_names = ("price", "amount", "total", "goods_total", "limit_amount", "unit_price", "rate_or_amount")
    # rova/matching/match.py is the vendored, byte-identical §9 matcher
    # (A2.14, B5.36 — FATAL if edited). Its internal `float(price)` is a
    # fuzzy-matching proximity heuristic over demo CSV data, never a ROVA
    # money/ledger field, so it is out of scope for this money-purity scan.
    vendored = SRC / "matching" / "match.py"
    offenders = []
    for path in SRC.rglob("*.py"):
        if path == vendored:
            continue
        text = path.read_text()
        for m in re.finditer(r"float\(\s*([a-zA-Z_.\[\]'\"]*)", text):
            arg = m.group(1).lower()
            if any(f in arg for f in money_field_names):
                offenders.append((str(path), m.group(0)))
    assert not offenders, f"float() applied to a money-looking field: {offenders}"


def test_quantize_requires_decimal():
    with pytest.raises(TypeError):
        quantize(1.5)  # type: ignore[arg-type]


def test_quantize_round_half_up():
    assert quantize(Decimal("1.005")) == Decimal("1.01")
    assert quantize(Decimal("1.004")) == Decimal("1.00")
    assert quantize(Decimal("2.5")) == Decimal("2.50")


def test_money_str_is_a_string_with_two_decimals():
    s = money_str(Decimal("10"))
    assert isinstance(s, str)
    assert s == "10.00"


def _decimal_column_names() -> set[str]:
    """Every column the DDL itself declares NUMERIC — derived by parsing
    migrations/sql/0001_initial.sql, not hand-copied, so this list can never
    drift out of date with the schema the way a hardcoded field-name list
    would (item 5's own 'generated programmatically, not hand-written'
    standard, applied here to item 1b's money-encoder proof)."""
    names = set(re.findall(r"^\s*([a-z_]+)\s+NUMERIC\(", DDL.read_text(), re.MULTILINE))
    assert len(names) >= 20, "sanity: the DDL parse should find the ~30 known NUMERIC columns"
    return names


def test_decimal_encoder_installed_stringifies_every_numeric_shape(client):
    """B fix 1b, direct proof: FastAPI's jsonable_encoder — the thing that
    actually serialises every route's return value — must render EVERY
    Decimal shape present in this schema (2dp money, 4dp rates/confidence,
    6dp lat/long) as a JSON string, not a number. The `client` fixture
    creates the real app (installing the encoder patch), so this exercises
    the same code path a live response goes through, not a reimplementation
    of it."""
    from fastapi.encoders import jsonable_encoder

    for value in (Decimal("850.00"), Decimal("0.0250"), Decimal("1"), Decimal("-25.969200"), Decimal("0.9000")):
        encoded = jsonable_encoder(value)
        assert isinstance(encoded, str), f"{value!r} encoded as {type(encoded).__name__}, not str: {encoded!r}"


def test_no_json_response_field_serialises_a_known_money_column_as_a_number(client, db_engine):
    """B fix 1b — 'a test that FAILS if any money field in any response
    comes back as a JSON number, not a test that checks one field': drives
    a real order through two live endpoints (POST checkout, GET order — the
    exact GET /v1/orders/{id} the review's own curl proof used) and scans
    the RAW response text of both for every column the schema itself
    declares NUMERIC (see `_decimal_column_names`, ~30 fields, not hand-
    picked) appearing as a bare, unquoted JSON number. `response.json()`
    round-tripping through Python would hide exactly this bug (both
    `"850.00"` and `850.0` become equal-looking values after `float()`-ish
    comparison) — reading `response.text` directly is what makes this a
    real regression guard for the literal defect the review proved live."""
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            "('org_money_v', '97001', 'VM', 'vm', 'VENDOR'), ('org_money_p', '97002', 'PM', 'pm', 'PHARMACY')"
        ))
        conn.execute(text(
            "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
            "delivery_mode, mov_amount, acceptance_mode, status) VALUES "
            "('ven_money', 'org_money_v', 'MAPUTO_CIDADE', 'VM', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0, "
            "'AUTO_ACCEPT_FULL', 'ACTIVE')"
        ))
        conn.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
            "latitude, longitude, status) VALUES "
            "('pha_money', 'org_money_p', 'MAPUTO_CIDADE', 'A', 'PM', 'X', -25.969200, 32.573200, 'ACTIVE')"
        ))
        conn.execute(text(
            "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
            "regulated_price, reviewer_ref, search_text) VALUES "
            "('idx_money', 'DM', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:xm', 'dm')"
        ))
        conn.execute(text(
            "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
            "pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES "
            "('ofr_money', 'ven_money', 'idx_money', false, 100, '1', 300, 85.00, now())"
        ))
        conn.execute(text(
            "INSERT INTO app_user (id, phone, name, password_hash) VALUES ('usr_money', :ph, 'Buyer', :h)"
        ), {"ph": "+258840000970", "h": hash_password("rova-demo")})
        conn.execute(text(
            "INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES "
            "('mem_money', 'usr_money', 'org_money_p', ARRAY['PharmacyBuyer'])"
        ))

    login = client.post("/v1/auth/login", json={"phone": "+258840000970", "password": "rova-demo", "surface": "PH"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    r = client.post("/v1/requests", json={"mode": "CATALOGUE"}, headers=headers)
    assert r.status_code == 200, r.text
    request_id = r.json()["id"]
    r = client.post(f"/v1/requests/{request_id}/lines",
                     json={"index_product_id": "idx_money", "qty_requested": 10}, headers=headers)
    assert r.status_code == 200, r.text

    money_columns = _decimal_column_names()

    checkout = client.post(f"/v1/requests/{request_id}/checkout",
                            headers={**headers, "Idempotency-Key": "money-test-key"})
    assert checkout.status_code == 200, checkout.text
    order_id = checkout.json()["orders"][0]["id"]

    order_resp = client.get(f"/v1/orders/{order_id}", headers=headers)
    assert order_resp.status_code == 200, order_resp.text

    offenders = []
    for label, raw in (("checkout response", checkout.text), ("GET order response", order_resp.text)):
        for col in money_columns:
            for m in re.finditer(rf'"{re.escape(col)}"\s*:\s*(-?\d[\d.eE+-]*)(?!")', raw):
                offenders.append(f"{label}: {col!r} = bare number {m.group(1)!r}")
    assert not offenders, "money/decimal field(s) serialised as a JSON number, not a string:\n" + "\n".join(offenders)
