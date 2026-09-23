"""Ops console, support chat, notifications and the assistant layer.

The console's contract is that it reads the platform without becoming a
second source of truth, and that history stays append-only. The chat's
contract is one thread per pharmacy with the vendor kept out of it. The
assistant's contract is that it never answers without saying what it
computed the answer from.
"""
from decimal import Decimal

import pytest
from sqlalchemy import text

from rova.auth.security import hash_password


def _accounts(db_engine, suffix: str) -> dict:
    with db_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
            f"('org_ov_{suffix}', 'TXOP{suffix}1', 'V', 'v', 'VENDOR'), "
            f"('org_op_{suffix}', 'TXOP{suffix}2', 'P', 'p', 'PHARMACY')"))
        conn.execute(text(
            "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
            "delivery_mode, mov_amount, acceptance_mode, status) VALUES "
            f"('ven_ov_{suffix}', 'org_ov_{suffix}', 'MAPUTO_CIDADE', 'V', 'DISTRIBUTOR', "
            "'VENDOR_OWN_FLEET', 0, 'MANUAL_CONFIRM', 'ACTIVE')"))
        conn.execute(text(
            "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, "
            "address, latitude, longitude, status) VALUES "
            f"('pha_op_{suffix}', 'org_op_{suffix}', 'MAPUTO_CIDADE', 'A', 'P', 'X', 0, 0, 'ACTIVE')"))

        def _user(uid, phone, org, roles):
            conn.execute(text("INSERT INTO app_user (id, phone, name, password_hash) VALUES (:i,:p,'U',:h)"),
                         {"i": uid, "p": phone, "h": hash_password("rova-demo")})
            conn.execute(text("INSERT INTO membership (id, user_id, organisation_id, role_codes) "
                              "VALUES (:m,:u,:o,:r)"),
                         {"m": f"mem_{uid}", "u": uid, "o": org, "r": roles})

        _user(f"usr_oph_{suffix}", f"+2588466{suffix}1", f"org_op_{suffix}",
              ["PharmacyAdmin", "PharmacyBuyer"])
        _user(f"usr_ovn_{suffix}", f"+2588466{suffix}2", f"org_ov_{suffix}", ["VendorAdmin"])
        _user(f"usr_oag_{suffix}", f"+2588466{suffix}3", None, ["SupportAgent"])
        _user(f"usr_oad_{suffix}", f"+2588466{suffix}4", None, ["PlatformAdmin"])
        _user(f"usr_ocf_{suffix}", f"+2588466{suffix}5", None, ["ComplianceOfficer"])
    return {"pharmacy_id": f"pha_op_{suffix}", "vendor_id": f"ven_ov_{suffix}",
            "ph": f"+2588466{suffix}1", "vn": f"+2588466{suffix}2", "ag": f"+2588466{suffix}3",
            "ad": f"+2588466{suffix}4", "cf": f"+2588466{suffix}5",
            "ph_user": f"usr_oph_{suffix}", "org": f"org_op_{suffix}"}


def _h(client, phone, surface="PH"):
    r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": surface})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ------------------------------------------------------------------ dashboard

def test_dashboard_returns_every_queue_count(client, db_engine):
    fx = _accounts(db_engine, "01")
    ad = _h(client, fx["ad"], "OP")
    r = client.get("/v1/ops/dashboard", headers=ad)
    assert r.status_code == 200, r.text
    counts = r.json()["counts"]
    for key in ("requests_normalizing", "orders_pending_acceptance", "disputes_open",
                "verifications_pending", "threads_awaiting_human", "timers_overdue"):
        assert key in counts
    assert isinstance(r.json()["invoices_outstanding_total"], str), "money is a string, never a float"


def test_a_pharmacy_cannot_open_the_ops_console(client, db_engine):
    fx = _accounts(db_engine, "02")
    ph = _h(client, fx["ph"])
    assert client.get("/v1/ops/dashboard", headers=ph).status_code == 403
    assert client.get("/v1/ops/audit-events", headers=ph).status_code == 403


def test_machines_endpoint_reflects_the_real_state_machines(client, db_engine):
    """The console renders this instead of a diagram that drifts, so it must
    come from the registry itself."""
    fx = _accounts(db_engine, "03")
    ad = _h(client, fx["ad"], "OP")
    r = client.get("/v1/ops/machines", headers=ad)
    assert r.status_code == 200
    codes = {m["code"] for m in r.json()["items"]}
    from rova.domain.machines.registry import MACHINES
    assert codes == set(MACHINES)
    sm06 = next(m for m in r.json()["items"] if m["code"] == "SM-06")
    approve = next(t for t in sm06["transitions"] if t["trigger"] == "APPROVE")
    assert approve["actors"] == ["ComplianceOfficer"]
    assert approve["has_guard"] is True


def test_the_console_has_no_write_route_for_history(client, db_engine):
    """audit_event, state_transition and ranking_isolation_audit are
    append-only. If a POST/PUT/PATCH/DELETE ever appears on one of these,
    that guarantee has quietly been given away."""
    from rova.main import create_app
    schema = create_app().openapi()
    for path, methods in schema["paths"].items():
        if any(s in path for s in ("audit-events", "state-transitions", "ranking-audits", "traceability")):
            assert set(methods) <= {"get"}, f"{path} exposes a write method"


# ------------------------------------------------------------------ config

def test_config_change_is_audited_and_type_checked(client, db_engine):
    fx = _accounts(db_engine, "04")
    ad = _h(client, fx["ad"], "OP")

    listed = client.get("/v1/ops/config", headers=ad, params={"key_prefix": "CFG-"})
    assert listed.status_code == 200
    assert listed.json()["items"], "config parameters are seeded rows, not env vars"

    key = "CFG-LOGIN-LOCKOUT-ATTEMPTS"
    detail = client.get(f"/v1/ops/config/{key}", headers=ad)
    assert detail.status_code == 200, detail.text
    assert detail.json()["resolved_value"] is not None

    bad = client.put(f"/v1/ops/config/{key}", headers=ad,
                     json={"value": "not-a-number", "reason": "test"})
    assert bad.status_code == 422, "a value that will not cast must be refused at the write, not at the read"

    ok = client.put(f"/v1/ops/config/{key}", headers=ad, json={"value": "7", "reason": "menos bloqueios"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["value"] == "7"

    with db_engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM audit_event WHERE action_code='CONFIG_PARAMETER_CHANGE' "
                              "AND subject_id=:i"), {"i": ok.json()["id"]}).scalar()
    assert n >= 1

    # put it back so other tests see the seeded value
    client.put(f"/v1/ops/config/{key}", headers=ad, json={"value": "5", "reason": "restore"})


def test_config_scope_rules_are_enforced(client, db_engine):
    fx = _accounts(db_engine, "05")
    ad = _h(client, fx["ad"], "OP")
    r = client.put("/v1/ops/config/CFG-LOGIN-LOCKOUT-ATTEMPTS", headers=ad,
                   json={"value": "5", "scope_type": "GLOBAL", "scope_id": "MAPUTO_CIDADE",
                         "reason": "x"})
    assert r.status_code == 422


def test_unknown_config_key_is_404(client, db_engine):
    fx = _accounts(db_engine, "06")
    ad = _h(client, fx["ad"], "OP")
    assert client.get("/v1/ops/config/CFG-DOES-NOT-EXIST", headers=ad).status_code == 404


# ------------------------------------------------------------------ mode switches

def test_a_mode_switch_cannot_be_flipped_without_passing_its_gates(client, db_engine):
    """R-140: `current_value` is written only inside SM-22, and only after the
    gates are checked and passed."""
    fx = _accounts(db_engine, "07")
    ad = _h(client, fx["ad"], "OP")
    switches = client.get("/v1/ops/mode-switches", headers=ad, params={"switch_key": "VENDOR_MODE"})
    assert switches.status_code == 200
    if not switches.json()["items"]:
        pytest.skip("no seeded VENDOR_MODE mode_switch row in the config-only seed")
    switch_id = switches.json()["items"][0]["id"]

    before = client.get(f"/v1/ops/mode-switches/{switch_id}", headers=ad).json()["current_value"]
    straight_to_flip = client.post(f"/v1/ops/mode-switches/{switch_id}/flip", headers=ad)
    assert straight_to_flip.status_code == 409, "FLIP is not legal from STEADY"

    after = client.get(f"/v1/ops/mode-switches/{switch_id}", headers=ad).json()["current_value"]
    assert after == before, "a refused flip must not have moved current_value"


def test_proposing_a_switch_does_not_change_the_current_value(client, db_engine):
    fx = _accounts(db_engine, "08")
    ad = _h(client, fx["ad"], "OP")
    switches = client.get("/v1/ops/mode-switches", headers=ad, params={"switch_key": "VENDOR_MODE"}).json()
    if not switches["items"]:
        pytest.skip("no seeded VENDOR_MODE mode_switch row")
    sw = switches["items"][0]
    if sw["status"] != "STEADY":
        pytest.skip("switch is mid-cycle from another test")

    r = client.post("/v1/ops/mode-switches", headers=ad,
                    json={"switch_key": "VENDOR_MODE", "proposed_value": "MULTI"})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "PROPOSED"
    assert r.json()["current_value"] == sw["current_value"], \
        "proposing must never write current_value (R-140)"


def test_only_a_platform_admin_may_touch_mode_switches(client, db_engine):
    fx = _accounts(db_engine, "09")
    cf = _h(client, fx["cf"], "OP")
    r = client.post("/v1/ops/mode-switches", headers=cf,
                    json={"switch_key": "VENDOR_MODE", "proposed_value": "MULTI"})
    assert r.status_code == 403


def test_latest_ranking_audit_says_plainly_when_none_has_run(client, db_engine):
    fx = _accounts(db_engine, "10")
    ad = _h(client, fx["ad"], "OP")
    r = client.get("/v1/ops/ranking-audits/latest", headers=ad)
    assert r.status_code == 200
    body = r.json()
    if body["audit"] is None:
        assert "cannot pass" in body["note"], \
            "with no audit on record the console must say the multi-vendor gate cannot pass"


# ------------------------------------------------------------------ support chat

def test_one_thread_per_pharmacy_forever(client, db_engine):
    fx = _accounts(db_engine, "11")
    ph = _h(client, fx["ph"])
    first = client.get("/v1/support/threads/me", headers=ph)
    assert first.status_code == 200, first.text
    second = client.get("/v1/support/threads/me", headers=ph)
    assert second.json()["id"] == first.json()["id"], "a pharmacy has exactly one conversation"
    assert first.json()["active_handler"] == "BOT"


def test_a_vendor_has_no_place_in_the_support_thread(client, db_engine):
    fx = _accounts(db_engine, "12")
    ph, vn = _h(client, fx["ph"]), _h(client, fx["vn"], "VN")
    thread_id = client.get("/v1/support/threads/me", headers=ph).json()["id"]
    r = client.get(f"/v1/support/threads/{thread_id}", headers=vn)
    assert r.status_code in (403, 404), "the vendor must never read this conversation"


def test_agent_cannot_reply_while_the_bot_still_holds_the_thread(client, db_engine):
    fx = _accounts(db_engine, "13")
    ph, ag = _h(client, fx["ph"]), _h(client, fx["ag"], "OP")
    thread_id = client.get("/v1/support/threads/me", headers=ph).json()["id"]
    r = client.post(f"/v1/support/threads/{thread_id}/messages", headers=ag,
                    json={"body": "olá"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GUARD_FAILED"


def test_escalate_take_over_answer_and_hand_back(client, db_engine):
    fx = _accounts(db_engine, "14")
    ph, ag = _h(client, fx["ph"]), _h(client, fx["ag"], "OP")

    client.post("/v1/support/threads/me/messages", headers=ph,
                json={"body": "a minha encomenda não chegou", "channel": "WHATSAPP_TEXT"})
    esc = client.post("/v1/support/threads/me/escalate", headers=ph)
    assert esc.status_code == 200, esc.text
    assert esc.json()["active_handler"] == "ESCALATING"
    assert esc.json()["active_ticket"] is not None, "escalating must open a ticket, not just change a label"
    thread_id, ticket_id = esc.json()["id"], esc.json()["active_ticket"]["id"]

    inbox = client.get("/v1/support/threads", headers=ag, params={"active_handler": "ESCALATING"})
    assert any(t["id"] == thread_id for t in inbox.json()["items"])

    taken = client.post(f"/v1/support/threads/{thread_id}/take-over", headers=ag)
    assert taken.status_code == 200, taken.text
    assert taken.json()["active_handler"] == "HUMAN"
    assert taken.json()["active_ticket"]["status"] == "IN_PROGRESS"

    reply = client.post(f"/v1/support/threads/{thread_id}/messages", headers=ag,
                        json={"body": "estou a verificar com o fornecedor"})
    assert reply.status_code == 201

    resolved = client.post(f"/v1/support/tickets/{ticket_id}/resolve", headers=ag,
                           json={"resolution_note": "entregue hoje às 14h"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["active_handler"] == "BOT", "the thread must go back to the bot once closed"
    bodies = [m["body"] for m in resolved.json()["messages"]]
    assert "entregue hoje às 14h" in bodies, \
        "the answer belongs in the conversation, not only in a ticket the pharmacy never opens"


def test_a_pharmacy_sees_only_its_own_tickets(client, db_engine):
    fx_a, fx_b = _accounts(db_engine, "15"), _accounts(db_engine, "16")
    ph_a, ph_b = _h(client, fx_a["ph"]), _h(client, fx_b["ph"])
    ticket = client.post("/v1/support/threads/me/escalate", headers=ph_a).json()["active_ticket"]["id"]
    assert client.get(f"/v1/support/tickets/{ticket}", headers=ph_b).status_code == 404
    assert not any(t["id"] == ticket for t in client.get("/v1/support/tickets", headers=ph_b).json()["items"])


def test_resolving_a_ticket_twice_is_refused(client, db_engine):
    fx = _accounts(db_engine, "17")
    ph, ag = _h(client, fx["ph"]), _h(client, fx["ag"], "OP")
    ticket = client.post("/v1/support/threads/me/escalate", headers=ph).json()["active_ticket"]["id"]
    thread_id = client.get("/v1/support/threads/me", headers=ph).json()["id"]
    client.post(f"/v1/support/threads/{thread_id}/take-over", headers=ag)
    body = {"resolution_note": "resolvido"}
    assert client.post(f"/v1/support/tickets/{ticket}/resolve", headers=ag, json=body).status_code == 200
    assert client.post(f"/v1/support/tickets/{ticket}/resolve", headers=ag, json=body).status_code == 409


# ------------------------------------------------------------------ notifications

def test_a_notification_is_only_visible_to_who_it_addresses(client, db_engine):
    from sqlalchemy.orm import sessionmaker
    from rova.notifications.router import emit

    fx_a, fx_b = _accounts(db_engine, "18"), _accounts(db_engine, "19")
    Session = sessionmaker(bind=db_engine, future=True)
    s = Session()
    emit(s, event_code="ORDER_DISPATCHED", recipient_user_id=fx_a["ph_user"],
         payload={"order": "x"}, rendered_text="A sua encomenda saiu")
    s.commit()
    s.close()

    ph_a, ph_b = _h(client, fx_a["ph"]), _h(client, fx_b["ph"])
    mine = client.get("/v1/notifications", headers=ph_a)
    assert mine.status_code == 200
    assert any(n["event_code"] == "ORDER_DISPATCHED" for n in mine.json()["items"])

    theirs = client.get("/v1/notifications", headers=ph_b).json()["items"]
    assert not any(n["recipient_user_id"] == fx_a["ph_user"] for n in theirs)


def test_marking_read_moves_the_unread_count(client, db_engine):
    from sqlalchemy.orm import sessionmaker
    from rova.notifications.router import emit

    fx = _accounts(db_engine, "20")
    Session = sessionmaker(bind=db_engine, future=True)
    s = Session()
    emit(s, event_code="INVOICE_READY", recipient_user_id=fx["ph_user"])
    s.commit()
    s.close()

    ph = _h(client, fx["ph"])
    before = client.get("/v1/notifications/unread-count", headers=ph).json()["unread"]
    assert before >= 1

    nid = next(n["id"] for n in client.get("/v1/notifications", headers=ph,
                                           params={"unread_only": True}).json()["items"])
    read = client.post(f"/v1/notifications/{nid}/read", headers=ph)
    assert read.status_code == 200
    assert read.json()["status"] == "READ"
    assert read.json()["read_at"] is not None

    after = client.get("/v1/notifications/unread-count", headers=ph).json()["unread"]
    assert after == before - 1


def test_dispatch_does_not_claim_to_have_sent_what_it_cannot_send(client, db_engine):
    """A channel with no adapter stays QUEUED. Marking it SENT would be a lie
    that only surfaces in a dispute."""
    from sqlalchemy.orm import sessionmaker
    from rova.notifications.router import emit

    fx = _accounts(db_engine, "21")
    Session = sessionmaker(bind=db_engine, future=True)
    s = Session()
    emit(s, event_code="ORDER_LATE", channel="SMS", recipient_user_id=fx["ph_user"])
    emit(s, event_code="ORDER_LATE", channel="IN_APP", recipient_user_id=fx["ph_user"])
    s.commit()
    s.close()

    ad = _h(client, fx["ad"], "OP")
    r = client.post("/v1/notifications/dispatch", headers=ad)
    assert r.status_code == 200, r.text
    assert r.json()["sent"] >= 1
    assert r.json()["left_queued_no_adapter"].get("SMS", 0) >= 1
    assert "SMS" not in r.json()["deliverable_channels"]


def test_there_is_no_endpoint_to_hand_write_a_notification(client, db_engine):
    """`emit()` is the only writer. A POST that creates an arbitrary
    notification would let anything address anyone."""
    from rova.main import create_app
    schema = create_app().openapi()
    assert "post" not in schema["paths"].get("/v1/notifications", {})


def test_a_pharmacy_cannot_read_the_whole_notification_queue(client, db_engine):
    fx = _accounts(db_engine, "22")
    ph = _h(client, fx["ph"])
    assert client.get("/v1/notifications-admin/queue", headers=ph).status_code == 403


# ------------------------------------------------------------------ assistant

def test_assistant_states_its_own_limits(client, db_engine):
    fx = _accounts(db_engine, "23")
    ph = _h(client, fx["ph"])
    r = client.get("/v1/assistant/capabilities", headers=ph)
    assert r.status_code == 200
    body = r.json()
    assert "give clinical, dosing or substitution advice" in body["does_not"]
    assert "place, change or cancel an order" in body["does_not"]
    assert "suggest a different price on a regulated product" in body["does_not"]


def test_replenishment_says_why_it_cannot_answer_without_a_pms_feed(client, db_engine):
    fx = _accounts(db_engine, "24")
    ph = _h(client, fx["ph"])
    r = client.get("/v1/assistant/replenishment", headers=ph)
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []
    assert "dispensing feed" in r.json()["reason"], \
        "with no feed it must say so, not infer consumption from purchases"


def test_order_patterns_refuses_to_describe_a_pattern_from_nothing(client, db_engine):
    fx = _accounts(db_engine, "25")
    ph = _h(client, fx["ph"])
    r = client.get("/v1/assistant/order-patterns", headers=ph)
    assert r.status_code == 200
    assert r.json()["confidence"] == "NONE"
    assert r.json()["items"] == []
    assert "not enough" in r.json()["reason"]


def test_a_pharmacy_cannot_ask_about_another_pharmacy(client, db_engine):
    fx_a, fx_b = _accounts(db_engine, "26"), _accounts(db_engine, "27")
    ph = _h(client, fx_a["ph"])
    for path in ("/v1/assistant/summary", "/v1/assistant/savings", "/v1/assistant/stock-risks"):
        r = client.get(path, headers=ph, params={"pharmacy_id": fx_b["pharmacy_id"]})
        assert r.status_code == 403, path


def test_savings_excludes_regulated_products_and_says_so(client, db_engine):
    fx = _accounts(db_engine, "28")
    ph = _h(client, fx["ph"])
    r = client.get("/v1/assistant/savings", headers=ph)
    assert r.status_code == 200, r.text
    assert "Diploma Ministerial 21/2017" in r.json()["regulated_note_pt"]
    assert "regulated_lines_excluded" in r.json()


def test_ask_routes_to_a_computation_and_names_it(client, db_engine):
    fx = _accounts(db_engine, "29")
    ph = _h(client, fx["ph"])
    r = client.post("/v1/assistant/ask", headers=ph, json={"question": "quando vou ficar sem stock?"})
    assert r.status_code == 200, r.text
    assert r.json()["intent"] == "replenish"
    assert r.json()["answered_by"] == "GET /v1/assistant/replenishment"


def test_ask_admits_when_it_does_not_know(client, db_engine):
    fx = _accounts(db_engine, "30")
    ph = _h(client, fx["ph"])
    r = client.post("/v1/assistant/ask", headers=ph,
                    json={"question": "que dose de amoxicilina devo dar a uma criança?"})
    assert r.status_code == 200
    assert r.json()["intent"] is None
    assert r.json()["answered_by"] is None
    assert r.json()["escalate_to"] == "POST /v1/support/threads/me/escalate", \
        "an unanswerable question must point at a human, not produce a guess"


def test_every_assistant_result_carries_its_basis(client, db_engine):
    fx = _accounts(db_engine, "31")
    ph = _h(client, fx["ph"])
    for path in ("/v1/assistant/summary", "/v1/assistant/savings", "/v1/assistant/replenishment"):
        body = client.get(path, headers=ph).json()
        assert "basis" in body, f"{path} answered without saying what it computed the answer from"


def test_summary_money_is_a_string(client, db_engine):
    fx = _accounts(db_engine, "32")
    ph = _h(client, fx["ph"])
    body = client.get("/v1/assistant/summary", headers=ph).json()
    assert isinstance(body["outstanding_to_vendors"], str)
    Decimal(body["outstanding_to_vendors"])
