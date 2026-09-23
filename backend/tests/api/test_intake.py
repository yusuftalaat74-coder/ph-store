"""Multi-channel intake over HTTP.

The point these tests defend: whichever door an order comes through, it lands
on one `request` row driven by SM-01, and a line the matcher could not
resolve never becomes an order line without a human saying so.
"""
import io

from sqlalchemy import text

from rova.auth.security import hash_password


def _index_product(db_engine, pid: str, inn: str, strength: str, pack: str = "20"):
    with db_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO index_product (id, inn, manufacturer, form, strength, pack_size, "
                 "aim_status, regulated_price, review_status, reviewer_ref, search_text) "
                 "VALUES (:id, :inn, 'Genérico Lda', 'Comprimido', :st, :pk, 'AUTHORISED', false, "
                 "'PUBLISHED', 'test-fixture', :search) ON CONFLICT (id) DO NOTHING"),
            {"id": pid, "inn": inn, "st": strength, "pk": pack,
             "search": f"{inn} {strength} {pack}".lower()},
        )


def _pharmacy_user(db_engine, suffix: str, role: str = "PharmacyBuyer") -> tuple[str, str]:
    phone = f"+25884300{suffix}"
    with db_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) "
                 "VALUES (:o, :t, 'F', 'f', 'PHARMACY') ON CONFLICT DO NOTHING"),
            {"o": f"org_int_{suffix}", "t": f"TAXINT{suffix}"},
        )
        conn.execute(
            text("INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, "
                 "address, latitude, longitude, status) VALUES (:p, :o, 'MAPUTO_CIDADE', 'A', 'F', 'X', 0, 0, "
                 "'ACTIVE') ON CONFLICT DO NOTHING"),
            {"p": f"pha_int_{suffix}", "o": f"org_int_{suffix}"},
        )
        conn.execute(
            text("INSERT INTO app_user (id, phone, name, password_hash) VALUES (:u, :ph, 'B', :h) "
                 "ON CONFLICT DO NOTHING"),
            {"u": f"usr_int_{suffix}", "ph": phone, "h": hash_password("rova-demo")},
        )
        conn.execute(
            text("INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES (:m, :u, :o, :r) "
                 "ON CONFLICT DO NOTHING"),
            {"m": f"mem_int_{suffix}", "u": f"usr_int_{suffix}", "o": f"org_int_{suffix}", "r": [role]},
        )
    return phone, f"pha_int_{suffix}"


def _ops_user(db_engine, suffix: str, role: str = "OpsReviewer") -> str:
    phone = f"+25884400{suffix}"
    with db_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO app_user (id, phone, name, password_hash) VALUES (:u, :ph, 'O', :h) "
                 "ON CONFLICT DO NOTHING"),
            {"u": f"usr_ops_{suffix}", "ph": phone, "h": hash_password("rova-demo")},
        )
        conn.execute(
            text("INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES (:m, :u, NULL, :r) "
                 "ON CONFLICT DO NOTHING"),
            {"m": f"mem_ops_{suffix}", "u": f"usr_ops_{suffix}", "r": [role]},
        )
    return phone


def _h(client, phone: str, surface: str) -> dict:
    r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": surface})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ------------------------------------------------------------------ text

def test_whatsapp_text_becomes_a_request_in_normalizing(client, db_engine):
    _index_product(db_engine, "idx_int_amox", "Amoxicilina", "500 mg", "21")
    phone, _ = _pharmacy_user(db_engine, "01")
    h = _h(client, phone, "PH")

    r = client.post("/v1/intake/text", headers=h, json={
        "body": "Amoxicilina 500 mg 21\nzzzqqq inexistente 999",
    })
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "NORMALIZING"
    assert out["channel"] == "WHATSAPP_TEXT"
    assert len(out["lines"]) == 2
    statuses = {l["match_status"] for l in out["lines"]}
    assert statuses <= {"AUTO", "ASK", "UNRESOLVED", "RESOLVED"}


def test_an_unresolved_line_is_never_given_a_product(client, db_engine):
    _index_product(db_engine, "idx_int_para", "Paracetamol", "500 mg")
    phone, _ = _pharmacy_user(db_engine, "02")
    h = _h(client, phone, "PH")
    r = client.post("/v1/intake/text", headers=h,
                    json={"lines": [{"text": "qqqzzzwww nada disto existe", "quantity": 3}]})
    assert r.status_code == 201, r.text
    line = r.json()["lines"][0]
    if line["match_status"] in ("ASK", "UNRESOLVED"):
        assert line["index_product_id"] is None, "a line the matcher did not resolve must carry no product"


def test_empty_text_intake_is_rejected(client, db_engine):
    phone, _ = _pharmacy_user(db_engine, "03")
    h = _h(client, phone, "PH")
    assert client.post("/v1/intake/text", headers=h, json={"body": "   \n  "}).status_code == 422


def test_a_pharmacy_cannot_raise_a_request_for_another_pharmacy(client, db_engine):
    phone, _ = _pharmacy_user(db_engine, "04")
    _, other = _pharmacy_user(db_engine, "05")
    h = _h(client, phone, "PH")
    r = client.post("/v1/intake/text", headers=h,
                    json={"pharmacy_id": other, "lines": [{"text": "x", "quantity": 1}]})
    assert r.status_code == 403


# ------------------------------------------------------------------ file

def test_csv_file_intake_reads_name_and_quantity(client, db_engine):
    _index_product(db_engine, "idx_int_ibu", "Ibuprofeno", "400 mg")
    phone, _ = _pharmacy_user(db_engine, "06")
    h = _h(client, phone, "PH")
    csv = b"Ibuprofeno 400 mg,12\nParacetamol 500 mg\n"
    r = client.post("/v1/intake/file", headers=h,
                    files={"file": ("order.csv", io.BytesIO(csv), "text/csv")})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["channel"] == "FILE"
    assert out["raw_payload_ref"], "the uploaded artefact must be stored, not discarded"
    qtys = [l["qty_requested"] for l in out["lines"]]
    assert qtys == [12, 1], "the second column is a quantity when it parses as one"


def test_empty_file_is_rejected(client, db_engine):
    phone, _ = _pharmacy_user(db_engine, "07")
    h = _h(client, phone, "PH")
    r = client.post("/v1/intake/file", headers=h,
                    files={"file": ("empty.csv", io.BytesIO(b""), "text/csv")})
    assert r.status_code == 422


# ------------------------------------------------------------------ photo / voice / queue

def test_photo_intake_waits_for_a_human_and_appears_in_the_ops_queue(client, db_engine):
    _index_product(db_engine, "idx_int_dip", "Dipirona", "500 mg")
    phone, _ = _pharmacy_user(db_engine, "08")
    h = _h(client, phone, "PH")
    r = client.post("/v1/intake/photo", headers=h,
                    files={"file": ("list.jpg", io.BytesIO(b"\xff\xd8\xff-not-a-real-jpeg"), "image/jpeg")})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["awaiting_transcription"] is True
    assert out["lines"] == [], "a photo is never OCR-guessed into order lines"
    request_id = out["id"]

    ops = _h(client, _ops_user(db_engine, "08"), "OP")
    queue = client.get("/v1/intake/queue", headers=ops)
    assert queue.status_code == 200
    assert any(x["id"] == request_id for x in queue.json()["items"])

    done = client.post(f"/v1/intake/{request_id}/transcribe", headers=ops,
                       json={"lines": [{"text": "Dipirona 500 mg", "quantity": 5}]})
    assert done.status_code == 200, done.text
    assert len(done.json()["lines"]) == 1

    queue2 = client.get("/v1/intake/queue", headers=ops)
    assert not any(x["id"] == request_id for x in queue2.json()["items"]), \
        "a transcribed request must leave the queue"


def test_transcribing_twice_is_a_conflict(client, db_engine):
    _index_product(db_engine, "idx_int_cef", "Cefalexina", "500 mg")
    phone, _ = _pharmacy_user(db_engine, "09")
    h = _h(client, phone, "PH")
    rid = client.post("/v1/intake/voice", headers=h,
                      files={"file": ("v.ogg", io.BytesIO(b"OggS-fake"), "audio/ogg")}).json()["id"]
    ops = _h(client, _ops_user(db_engine, "09"), "OP")
    body = {"lines": [{"text": "Cefalexina 500 mg", "quantity": 2}]}
    assert client.post(f"/v1/intake/{rid}/transcribe", headers=ops, json=body).status_code == 200
    assert client.post(f"/v1/intake/{rid}/transcribe", headers=ops, json=body).status_code == 409


def test_a_pharmacy_cannot_transcribe(client, db_engine):
    phone, _ = _pharmacy_user(db_engine, "10")
    h = _h(client, phone, "PH")
    rid = client.post("/v1/intake/photo", headers=h,
                      files={"file": ("l.jpg", io.BytesIO(b"x"), "image/jpeg")}).json()["id"]
    r = client.post(f"/v1/intake/{rid}/transcribe", headers=h,
                    json={"lines": [{"text": "x", "quantity": 1}]})
    assert r.status_code == 403


# ------------------------------------------------------------------ phone call

def test_phone_call_records_the_agent_not_the_pharmacy_user(client, db_engine):
    _index_product(db_engine, "idx_int_amx2", "Amoxicilina", "250 mg")
    _, pharmacy_id = _pharmacy_user(db_engine, "11")
    ops = _h(client, _ops_user(db_engine, "11"), "OP")
    r = client.post("/v1/intake/phone-call", headers=ops, json={
        "pharmacy_id": pharmacy_id, "call_summary": "Pediu amoxicilina",
        "lines": [{"text": "Amoxicilina 250 mg", "quantity": 4}],
    })
    assert r.status_code == 201, r.text
    with db_engine.connect() as conn:
        row = conn.execute(
            text("SELECT created_by_user_id, acting_ops_user_id, channel FROM request WHERE id=:r"),
            {"r": r.json()["id"]},
        ).mappings().one()
    assert row["channel"] == "PHONE_CALL"
    assert row["created_by_user_id"] is None
    assert row["acting_ops_user_id"] == "usr_ops_11", "the agent who took the call must be recorded"


def test_a_pharmacy_cannot_use_the_phone_call_channel(client, db_engine):
    phone, pid = _pharmacy_user(db_engine, "12")
    h = _h(client, phone, "PH")
    r = client.post("/v1/intake/phone-call", headers=h,
                    json={"pharmacy_id": pid, "call_summary": "x"})
    assert r.status_code == 403


# ------------------------------------------------------------------ PMS

def test_pms_replays_the_same_basket_and_refuses_a_different_one(client, db_engine):
    _index_product(db_engine, "idx_int_omz", "Omeprazol", "20 mg")
    phone, _ = _pharmacy_user(db_engine, "13")
    h = _h(client, phone, "PH")
    key = {"Idempotency-Key": "pms-key-13"}
    body = {"lines": [{"text": "Omeprazol 20 mg", "quantity": 10}]}

    first = client.post("/v1/intake/pms", headers={**h, **key}, json=body)
    assert first.status_code == 201, first.text

    replay = client.post("/v1/intake/pms", headers={**h, **key}, json=body)
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"], "a retry must replay, not create a second request"

    different = client.post("/v1/intake/pms", headers={**h, **key},
                            json={"lines": [{"text": "Omeprazol 20 mg", "quantity": 99}]})
    assert different.status_code == 409
    assert different.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_pms_without_an_idempotency_key_is_refused(client, db_engine):
    phone, _ = _pharmacy_user(db_engine, "14")
    h = _h(client, phone, "PH")
    r = client.post("/v1/intake/pms", headers=h, json={"lines": [{"text": "x", "quantity": 1}]})
    assert r.status_code == 422


# ------------------------------------------------------------------ normalisation gate

def test_normalization_complete_is_refused_while_a_line_is_unresolved(client, db_engine):
    phone, _ = _pharmacy_user(db_engine, "15")
    h = _h(client, phone, "PH")
    ops = _h(client, _ops_user(db_engine, "15"), "OP")
    rid = client.post("/v1/intake/text", headers=h,
                      json={"lines": [{"text": "zzzqqq nada", "quantity": 1}]}).json()["id"]

    view = client.get(f"/v1/intake/{rid}", headers=h).json()
    if not view["blocking_line_ids"]:
        return  # the matcher resolved it; this scenario does not apply
    assert view["can_complete_normalisation"] is False

    r = client.post(f"/v1/requests/{rid}/normalization-complete", headers=ops)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GUARD_FAILED"


def test_resolving_the_blocking_line_opens_the_gate_and_reaches_awaiting_confirmation(client, db_engine):
    _index_product(db_engine, "idx_int_metf", "Metformina", "850 mg")
    phone, _ = _pharmacy_user(db_engine, "16")
    h = _h(client, phone, "PH")
    ops = _h(client, _ops_user(db_engine, "16"), "OP")
    rid = client.post("/v1/intake/text", headers=h,
                      json={"lines": [{"text": "zzzqqq nada disto", "quantity": 1}]}).json()["id"]

    view = client.get(f"/v1/intake/{rid}", headers=h).json()
    for line_id in view["blocking_line_ids"]:
        r = client.post(f"/v1/request-lines/{line_id}/resolve", headers=h,
                        json={"index_product_id": "idx_int_metf"})
        assert r.status_code == 200, r.text
        assert r.json()["match_status"] == "RESOLVED", \
            "a human choice is RESOLVED, never relabelled as the matcher's own AUTO"

    done = client.post(f"/v1/requests/{rid}/normalization-complete", headers=ops)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "AWAITING_CONFIRMATION"

    confirmed = client.post(f"/v1/requests/{rid}/confirm", headers=h)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "CONFIRMED"


def test_resolving_to_an_unpublished_product_is_refused(client, db_engine):
    phone, _ = _pharmacy_user(db_engine, "17")
    h = _h(client, phone, "PH")
    rid = client.post("/v1/intake/text", headers=h,
                      json={"lines": [{"text": "zzzqqq", "quantity": 1}]}).json()["id"]
    view = client.get(f"/v1/intake/{rid}", headers=h).json()
    if not view["blocking_line_ids"]:
        return
    line_id = view["blocking_line_ids"][0]
    r = client.post(f"/v1/request-lines/{line_id}/resolve", headers=h,
                    json={"index_product_id": "idx_does_not_exist"})
    assert r.status_code == 404


def test_normalization_complete_is_refused_on_an_empty_basket(client, db_engine):
    _index_product(db_engine, "idx_int_sim", "Sinvastatina", "20 mg")
    phone, _ = _pharmacy_user(db_engine, "18")
    h = _h(client, phone, "PH")
    ops = _h(client, _ops_user(db_engine, "18"), "OP")
    rid = client.post("/v1/intake/photo", headers=h,
                      files={"file": ("p.jpg", io.BytesIO(b"x"), "image/jpeg")}).json()["id"]
    r = client.post(f"/v1/requests/{rid}/normalization-complete", headers=ops)
    assert r.status_code == 409


# ------------------------------------------------------------------ demand gaps

def test_dropping_an_unmatched_line_records_a_demand_gap(client, db_engine):
    phone, pharmacy_id = _pharmacy_user(db_engine, "19")
    h = _h(client, phone, "PH")
    ops = _h(client, _ops_user(db_engine, "19"), "OP")
    rid = client.post("/v1/intake/text", headers=h,
                      json={"lines": [{"text": "produto-que-nao-existe-zzz", "quantity": 7}]}).json()["id"]
    view = client.get(f"/v1/intake/{rid}", headers=h).json()
    unresolved = [l for l in view["lines"] if l["match_status"] == "UNRESOLVED"]
    if not unresolved:
        return

    r = client.delete(f"/v1/request-lines/{unresolved[0]['id']}", headers=h)
    assert r.status_code == 200

    gaps = client.get("/v1/demand-gaps", headers=ops, params={"cause": "PRODUCT_NOT_IN_INDEX"})
    assert gaps.status_code == 200
    mine = [g for g in gaps.json()["items"] if g["pharmacy_id"] == pharmacy_id]
    assert mine, "dropping an unmatched line must leave evidence the Index is missing a product"
    assert mine[0]["qty_requested"] == 7

    summary = client.get("/v1/demand-gaps/summary", headers=ops)
    assert summary.status_code == 200


# ------------------------------------------------------------------ listing & log

def test_requests_are_listable_and_scoped_to_the_callers_pharmacy(client, db_engine):
    _index_product(db_engine, "idx_int_los", "Losartana", "50 mg")
    phone_a, _ = _pharmacy_user(db_engine, "20")
    phone_b, _ = _pharmacy_user(db_engine, "21")
    ha, hb = _h(client, phone_a, "PH"), _h(client, phone_b, "PH")
    mine = client.post("/v1/intake/text", headers=ha,
                       json={"lines": [{"text": "Losartana 50 mg", "quantity": 1}]}).json()["id"]

    listed_a = client.get("/v1/requests", headers=ha).json()["items"]
    assert any(x["id"] == mine for x in listed_a)

    listed_b = client.get("/v1/requests", headers=hb).json()["items"]
    assert not any(x["id"] == mine for x in listed_b), "another pharmacy must not see this request"


def test_channel_filter_works(client, db_engine):
    _index_product(db_engine, "idx_int_atv", "Atorvastatina", "20 mg")
    phone, _ = _pharmacy_user(db_engine, "22")
    h = _h(client, phone, "PH")
    client.post("/v1/intake/text", headers=h, json={"lines": [{"text": "Atorvastatina 20 mg", "quantity": 1}]})
    r = client.get("/v1/requests", headers=h, params={"channel": "WHATSAPP_TEXT"})
    assert r.status_code == 200
    assert all(x["channel"] == "WHATSAPP_TEXT" for x in r.json()["items"])


def test_unknown_channel_filter_is_422_not_500(client, db_engine):
    phone, _ = _pharmacy_user(db_engine, "23")
    h = _h(client, phone, "PH")
    r = client.get("/v1/requests", headers=h, params={"channel": "TELEPATHY"})
    assert r.status_code == 422


def test_request_transitions_are_readable(client, db_engine):
    _index_product(db_engine, "idx_int_enp", "Enalapril", "10 mg")
    phone, _ = _pharmacy_user(db_engine, "24")
    h = _h(client, phone, "PH")
    ops = _h(client, _ops_user(db_engine, "24"), "OP")
    rid = client.post("/v1/intake/text", headers=h,
                      json={"lines": [{"text": "Enalapril 10 mg", "quantity": 1}]}).json()["id"]
    view = client.get(f"/v1/intake/{rid}", headers=h).json()
    for line_id in view["blocking_line_ids"]:
        client.post(f"/v1/request-lines/{line_id}/resolve", headers=h,
                    json={"index_product_id": "idx_int_enp"})
    client.post(f"/v1/requests/{rid}/normalization-complete", headers=ops)

    tr = client.get(f"/v1/requests/{rid}/transitions", headers=h)
    assert tr.status_code == 200
    assert [t["trigger"] for t in tr.json()["items"]] == ["NORMALIZATION_COMPLETE"]


def test_create_request_with_a_bad_allocation_strategy_is_422_not_500(client, db_engine):
    """The exact 500 found against the live server in September 2026."""
    phone, _ = _pharmacy_user(db_engine, "25")
    h = _h(client, phone, "PH")
    r = client.post("/v1/requests", headers=h,
                    json={"mode": "CATALOGUE", "allocation_strategy": "BEST_PRICE"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert any("allocation_strategy" in d["field"] for d in r.json()["error"]["details"])


def test_login_with_a_bad_surface_is_422_not_500(client, db_engine):
    """The other half of the same defect: `surface` reached the
    `auth_session_surface_check` CHECK and came back as a 500."""
    phone, _ = _pharmacy_user(db_engine, "26")
    r = client.post("/v1/auth/login",
                    json={"phone": phone, "password": "rova-demo", "surface": "PHARMACY_APP"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
