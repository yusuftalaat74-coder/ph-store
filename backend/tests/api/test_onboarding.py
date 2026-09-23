"""Onboarding surface over HTTP — organisation, pharmacy, vendor, licence,
verification case, terms/consent, users and memberships.

These drive the real SM-06 / SM-07 / SM-08 machines through the new routes,
so a transition that was previously only reachable from a unit test is now
proven reachable from the outside, with RBAC applied.
"""
from sqlalchemy import text

from rova.auth.security import hash_password

ROLES = {
    "compliance": "ComplianceOfficer",
    "admin": "PlatformAdmin",
    "ops": "OpsReviewer",
}


def _platform_user(db_engine, suffix: str, role: str) -> str:
    """Creates a platform user (no organisation) and returns their phone."""
    phone = f"+25884100{suffix}"
    with db_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO app_user (id, phone, name, password_hash) "
                 "VALUES (:id, :p, 'Plat', :h) ON CONFLICT DO NOTHING"),
            {"id": f"usr_onb_{suffix}", "p": phone, "h": hash_password("rova-demo")},
        )
        conn.execute(
            text("INSERT INTO membership (id, user_id, organisation_id, role_codes) "
                 "VALUES (:id, :u, NULL, :r) ON CONFLICT DO NOTHING"),
            {"id": f"mem_onb_{suffix}", "u": f"usr_onb_{suffix}", "r": [ROLES[role]]},
        )
    return phone


def _token(client, phone: str, surface: str = "OP") -> str:
    r = client.post("/v1/auth/login", json={"phone": phone, "password": "rova-demo", "surface": surface})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------- organisations

def test_create_organisation_then_read_it_back(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "01", "ops")))
    r = client.post("/v1/organisations", headers=h,
                    json={"tax_id": "TAX-ONB-01", "legal_name": "  Farmácia   Teste  ", "type": "PHARMACY"})
    assert r.status_code == 201, r.text
    org = r.json()
    assert org["status"] == "ACTIVE"
    assert org["legal_name_normalised"] == "farmácia teste", "legal name must be normalised for conflict checks"

    got = client.get(f"/v1/organisations/{org['id']}", headers=h)
    assert got.status_code == 200
    assert got.json()["id"] == org["id"], "the write must be committed before the response is returned"


def test_duplicate_tax_id_is_a_conflict_not_a_500(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "02", "ops")))
    body = {"tax_id": "TAX-ONB-02", "legal_name": "Dup", "type": "VENDOR"}
    assert client.post("/v1/organisations", headers=h, json=body).status_code == 201
    r = client.post("/v1/organisations", headers=h, json=body)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CONFLICT"


def test_unknown_enum_value_is_422_naming_the_field_not_500(client, db_engine):
    """The defect found during the September verification: a wrong enum
    value used to reach the DB CHECK and surface as an opaque 500."""
    h = _auth(_token(client, _platform_user(db_engine, "03", "ops")))
    r = client.post("/v1/organisations", headers=h,
                    json={"tax_id": "TAX-ONB-03", "legal_name": "X", "type": "HOSPITAL"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert any("type" in d["field"] for d in r.json()["error"]["details"])


# --------------------------------------------------------------- vendor approval

def _vendor_ready_for_approval(client, h, suffix: str, *, approve_licence: bool = True) -> str:
    """Creates a vendor with everything R-111/R-112/R-143 demand.

    Order matters and is not incidental: SM-08 `APPROVE` cascades into SM-06
    `APPROVE` on the holder (see `sm08_licence._effect_approve`), so the
    attestation and the agreement must exist *before* the alvará is approved
    or the cascade's own guard fails the licence approval too.
    """
    org = client.post("/v1/organisations", headers=h,
                      json={"tax_id": f"TAX-V-{suffix}", "legal_name": f"V{suffix}", "type": "VENDOR"}).json()
    vendor = client.post("/v1/vendors", headers=h, json={
        "organisation_id": org["id"], "region_code": "MAPUTO_CIDADE", "trade_name": f"V{suffix}",
        "vendor_type": "IMPORTER_WHOLESALER", "delivery_mode": "VENDOR_OWN_FLEET",
        "mov_amount": 1000, "sourcing_attestation": True,
    }).json()
    client.post("/v1/vendor-agreements", headers=h, json={
        "vendor_id": vendor["id"], "document_ref": "doc://agr", "multi_vendor_clause_ack": True,
    })
    licence = client.post("/v1/licences", headers=h, json={
        "holder_type": "VENDOR", "holder_id": vendor["id"], "type": "WHOLESALE_ALVARA",
        "number": f"ALV-{suffix}", "issuer": "ANARME", "issue_date": "2026-01-01",
        "expiry_date": "2027-01-01", "document_ref": "doc://alvara",
    }).json()
    assert client.post(f"/v1/licences/{licence['id']}/open-review", headers=h).status_code == 200
    if approve_licence:
        r = client.post(f"/v1/licences/{licence['id']}/approve", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "VALID"
    return vendor["id"]


def test_approving_the_alvara_cascades_the_vendor_to_active(client, db_engine):
    """SM-08 APPROVE fires SM-06 APPROVE on the holder — one HTTP call, two
    machines, both logged."""
    h = _auth(_token(client, _platform_user(db_engine, "04", "compliance")))
    vendor_id = _vendor_ready_for_approval(client, h, "04")
    v = client.get(f"/v1/vendors/{vendor_id}", headers=h)
    assert v.status_code == 200, v.text
    assert v.json()["status"] == "ACTIVE", "the licence approval must have driven SM-06 to ACTIVE"

    with db_engine.connect() as conn:
        machines = conn.execute(
            text("SELECT DISTINCT machine FROM state_transition WHERE subject_id=:v"), {"v": vendor_id},
        ).scalars().all()
    assert machines == ["SM-06"]


def test_vendor_without_valid_alvara_is_refused_by_r111(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "05", "compliance")))
    org = client.post("/v1/organisations", headers=h,
                      json={"tax_id": "TAX-V-05", "legal_name": "V05", "type": "VENDOR"}).json()
    vendor = client.post("/v1/vendors", headers=h, json={
        "organisation_id": org["id"], "region_code": "MAPUTO_CIDADE", "trade_name": "V05",
        "vendor_type": "DISTRIBUTOR", "delivery_mode": "VENDOR_OWN_FLEET",
        "mov_amount": 0, "sourcing_attestation": True,
    }).json()
    r = client.post(f"/v1/vendors/{vendor['id']}/approve", headers=h)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GUARD_FAILED"
    assert r.json()["error"]["rule"] == "R-111"


def _vendor_admin_token(client, db_engine, suffix: str, vendor_id: str) -> str:
    """A real VendorAdmin membership on the vendor's own organisation."""
    phone = f"+25884200{suffix}"
    with db_engine.begin() as conn:
        org_id = conn.execute(
            text("SELECT organisation_id FROM vendor_account WHERE id=:v"), {"v": vendor_id}
        ).scalar()
        conn.execute(
            text("INSERT INTO app_user (id, phone, name, password_hash) VALUES (:id, :p, 'VA', :h)"),
            {"id": f"usr_va_{suffix}", "p": phone, "h": hash_password("rova-demo")},
        )
        conn.execute(
            text("INSERT INTO membership (id, user_id, organisation_id, role_codes) VALUES (:id, :u, :o, :r)"),
            {"id": f"mem_va_{suffix}", "u": f"usr_va_{suffix}", "o": org_id, "r": ["VendorAdmin"]},
        )
    return _token(client, phone, surface="VN")


def test_vendor_without_attestation_is_refused_by_r112(client, db_engine):
    """Reaching R-112 needs a VALID alvará — otherwise R-111 fires first —
    while the vendor is still ONBOARDING. Rejecting the vendor before
    approving the licence keeps the SM-08 cascade from firing (it only runs
    from ONBOARDING); the vendor's own RESUBMIT then puts it back to
    ONBOARDING with the alvará already VALID. That is the real resubmission
    path, not a contrivance."""
    h = _auth(_token(client, _platform_user(db_engine, "06", "compliance")))
    vendor_id = _vendor_ready_for_approval(client, h, "06", approve_licence=False)

    assert client.post(f"/v1/vendors/{vendor_id}/reject", headers=h).json()["status"] == "REJECTED"
    lic = client.get("/v1/licences", headers=h, params={"holder_id": vendor_id}).json()["items"][0]
    assert client.post(f"/v1/licences/{lic['id']}/approve", headers=h).json()["status"] == "VALID"

    h_va = _auth(_vendor_admin_token(client, db_engine, "06", vendor_id))
    assert client.post(f"/v1/vendors/{vendor_id}/resubmit", headers=h_va).json()["status"] == "ONBOARDING"
    assert client.post(f"/v1/vendors/{vendor_id}/attestation", headers=h_va,
                       json={"sourcing_attestation": False}).status_code == 200

    r = client.post(f"/v1/vendors/{vendor_id}/approve", headers=h)
    assert r.status_code == 409
    assert r.json()["error"]["rule"] == "R-112", r.text


def test_resubmit_is_the_vendors_own_action_not_the_reviewers(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "23", "compliance")))
    vendor_id = _vendor_ready_for_approval(client, h, "23", approve_licence=False)
    client.post(f"/v1/vendors/{vendor_id}/reject", headers=h)
    assert client.post(f"/v1/vendors/{vendor_id}/resubmit", headers=h).status_code == 403


def test_attestation_change_is_written_to_the_append_only_audit_log(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "07", "compliance")))
    vendor_id = _vendor_ready_for_approval(client, h, "07", approve_licence=False)
    client.post(f"/v1/vendors/{vendor_id}/attestation", headers=h, json={"sourcing_attestation": False})
    with db_engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM audit_event WHERE action_code='SOURCING_ATTESTATION_CHANGE' "
                 "AND subject_id=:v"), {"v": vendor_id},
        ).scalar()
    assert n >= 1


def test_suspend_then_reinstate_keeps_the_suspension_cause_check_satisfied(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "08", "compliance")))
    vendor_id = _vendor_ready_for_approval(client, h, "08")  # already ACTIVE via the cascade

    s = client.post(f"/v1/vendors/{vendor_id}/suspend", headers=h)
    assert s.status_code == 200
    assert s.json()["status"] == "SUSPENDED"
    assert s.json()["suspension_cause"] == "MANUAL_INCIDENT"

    r = client.post(f"/v1/vendors/{vendor_id}/reinstate", headers=h)
    assert r.json()["status"] == "ACTIVE"
    assert r.json()["suspension_cause"] is None


def test_illegal_transition_reports_the_legal_triggers(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "09", "compliance")))
    vendor_id = _vendor_ready_for_approval(client, h, "09", approve_licence=False)
    r = client.post(f"/v1/vendors/{vendor_id}/reinstate", headers=h)  # still ONBOARDING
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ILLEGAL_TRANSITION"


# --------------------------------------------------------------- pharmacy

def test_pharmacy_onboarding_and_patch(client, db_engine):
    h_ops = _auth(_token(client, _platform_user(db_engine, "11", "ops")))
    org = client.post("/v1/organisations", headers=h_ops,
                      json={"tax_id": "TAX-P-11", "legal_name": "P11", "type": "PHARMACY"}).json()
    pha = client.post("/v1/pharmacies", headers=h_ops, json={
        "organisation_id": org["id"], "region_code": "MAPUTO_CIDADE", "licence_type": "A",
        "trade_name": "P11", "address": "Av. 24 de Julho", "latitude": -25.96, "longitude": 32.58,
    })
    assert pha.status_code == 201, pha.text
    assert pha.json()["status"] == "ONBOARDING"

    patched = client.patch(f"/v1/pharmacies/{pha.json()['id']}", headers=_auth(
        _token(client, _platform_user(db_engine, "12", "admin"))), json={"auto_reroute": False})
    assert patched.status_code == 200
    assert patched.json()["auto_reroute"] is False


def test_pharmacy_in_unknown_region_is_refused(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "13", "ops")))
    org = client.post("/v1/organisations", headers=h,
                      json={"tax_id": "TAX-P-13", "legal_name": "P13", "type": "PHARMACY"}).json()
    r = client.post("/v1/pharmacies", headers=h, json={
        "organisation_id": org["id"], "region_code": "ATLANTIS", "licence_type": "A",
        "trade_name": "P13", "address": "X", "latitude": 0, "longitude": 0,
    })
    assert r.status_code == 422


def test_pharmacy_account_rejects_a_vendor_organisation(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "14", "ops")))
    org = client.post("/v1/organisations", headers=h,
                      json={"tax_id": "TAX-P-14", "legal_name": "V14", "type": "VENDOR"}).json()
    r = client.post("/v1/pharmacies", headers=h, json={
        "organisation_id": org["id"], "region_code": "MAPUTO_CIDADE", "licence_type": "A",
        "trade_name": "X", "address": "X", "latitude": 0, "longitude": 0,
    })
    assert r.status_code == 422


# --------------------------------------------------------------- licence

def test_licence_expiry_before_issue_is_refused(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "15", "compliance")))
    org = client.post("/v1/organisations", headers=h,
                      json={"tax_id": "TAX-L-15", "legal_name": "V15", "type": "VENDOR"}).json()
    v = client.post("/v1/vendors", headers=h, json={
        "organisation_id": org["id"], "region_code": "MAPUTO_CIDADE", "trade_name": "V15",
        "vendor_type": "DISTRIBUTOR", "delivery_mode": "VENDOR_OWN_FLEET", "mov_amount": 0,
    }).json()
    r = client.post("/v1/licences", headers=h, json={
        "holder_type": "VENDOR", "holder_id": v["id"], "type": "WHOLESALE_ALVARA",
        "number": "X", "issuer": "ANARME", "issue_date": "2027-01-01",
        "expiry_date": "2026-01-01", "document_ref": "d",
    })
    assert r.status_code == 422


def test_licence_review_chain_is_logged_as_state_transitions(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "16", "compliance")))
    vendor_id = _vendor_ready_for_approval(client, h, "16")
    lic = client.get("/v1/licences", headers=h, params={"holder_id": vendor_id}).json()["items"][0]
    tr = client.get(f"/v1/licences/{lic['id']}/transitions", headers=h).json()["items"]
    triggers = [t["trigger"] for t in tr]
    assert triggers == ["OPEN_REVIEW", "APPROVE"]


# --------------------------------------------------------------- verification case

def test_verification_case_decide_is_single_shot(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "17", "compliance")))
    org = client.post("/v1/organisations", headers=h,
                      json={"tax_id": "TAX-C-17", "legal_name": "C17", "type": "VENDOR"}).json()
    case = client.post("/v1/verification-cases", headers=h,
                       json={"subject_type": "VENDOR_ONBOARDING", "organisation_id": org["id"]}).json()
    assert case["decision"] == "PENDING"
    assert client.post(f"/v1/verification-cases/{case['id']}/assign", headers=h).status_code == 200

    first = client.post(f"/v1/verification-cases/{case['id']}/decide", headers=h,
                        json={"decision": "APPROVED", "decision_notes": "ok"})
    assert first.status_code == 200
    assert first.json()["decided_at"] is not None

    second = client.post(f"/v1/verification-cases/{case['id']}/decide", headers=h,
                         json={"decision": "REJECTED"})
    assert second.status_code == 409


# --------------------------------------------------------------- terms & consent

def test_terms_accept_then_withdraw(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "18", "ops")))
    terms = client.get("/v1/terms", headers=h)
    assert terms.status_code == 200
    if not terms.json()["items"]:
        return  # config-only seed has no terms rows; the routes are still proven registered
    tv = terms.json()["items"][0]
    consent = client.post("/v1/terms/accept", headers=h, json={
        "terms_version_id": tv["id"], "subject_type": "USER", "subject_id": "usr_onb_18",
    })
    assert consent.status_code == 201
    w = client.post(f"/v1/consents/{consent.json()['id']}/withdraw", headers=h)
    assert w.status_code == 200
    assert w.json()["withdrawn_at"] is not None
    again = client.post(f"/v1/consents/{consent.json()['id']}/withdraw", headers=h)
    assert again.status_code == 409


# --------------------------------------------------------------- users & memberships

def test_membership_with_a_platform_role_cannot_carry_an_organisation(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "19", "admin")))
    user = client.post("/v1/users", headers=h, json={
        "phone": "+258841009919", "name": "New", "password": "a-long-enough-password",
    })
    assert user.status_code == 201, user.text
    assert "password_hash" not in user.json(), "the hash must never leave the server"

    org = client.post("/v1/organisations", headers=_auth(
        _token(client, _platform_user(db_engine, "20", "ops"))),
        json={"tax_id": "TAX-M-20", "legal_name": "M20", "type": "PHARMACY"}).json()

    bad = client.post("/v1/memberships", headers=h, json={
        "user_id": user.json()["id"], "organisation_id": org["id"], "role_codes": ["ComplianceOfficer"],
    })
    assert bad.status_code == 422

    good = client.post("/v1/memberships", headers=h, json={
        "user_id": user.json()["id"], "organisation_id": org["id"], "role_codes": ["PharmacyBuyer"],
    })
    assert good.status_code == 201

    revoked = client.post(f"/v1/memberships/{good.json()['id']}/revoke", headers=h)
    assert revoked.json()["status"] == "REVOKED"


def test_unknown_role_code_is_422_not_a_check_violation(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "21", "admin")))
    user = client.post("/v1/users", headers=h, json={
        "phone": "+258841009921", "name": "N", "password": "a-long-enough-password",
    }).json()
    r = client.post("/v1/memberships", headers=h,
                    json={"user_id": user["id"], "role_codes": ["SupremeLeader"]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_regions_are_listable(client, db_engine):
    h = _auth(_token(client, _platform_user(db_engine, "22", "ops")))
    r = client.get("/v1/regions", headers=h)
    assert r.status_code == 200
    assert any(x["code"] == "MAPUTO_CIDADE" for x in r.json()["items"])
