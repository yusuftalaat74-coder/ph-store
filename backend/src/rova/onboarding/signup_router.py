"""A pharmacy creates its own account from the phone, and a reviewer decides.

Signup spec (`phstore-office-spec/signup/SPEC.md`), §2. Until now every row a
pharmacy needs — organisation, pharmacy_account, licence, app_user,
membership — could only be written by platform staff through
`onboarding/router.py`. This module composes the same rows in one request
transaction for the pharmacist himself, and gives the review team one screen
to approve or reject what arrives.

What the design leans on:

* Nothing that commits money happens before approval. The new account can
  browse and fill a cart at once; `checkout()` refuses anything that is not
  ACTIVE/LICENCE_EXPIRING (`PHARMACY_NOT_ACTIVE`, §2.7).
* Every state change still goes through `MACHINES[...]` — SM-08 for the
  licence, whose APPROVE effect drives SM-07 for the pharmacy — so the
  `state_transition` log and the R-113 guard hold exactly as before.
* Sign-up is the only unauthenticated write, so it is rate limited per IP and
  per phone from `signup_attempt` (two uvicorn workers: process memory would
  count half). Attempt rows are committed before an error is raised, because
  `get_session()` rolls the request back on any exception — the same trap
  the login lockout fell into once (auth/router.py, B fix 1a).
* Files are typed by their first bytes, never by name or `content_type`, and
  served only through `GET /v1/licences/{id}/document` with role and
  ownership checks — never from a static mount.

This router is included in `main.py` BEFORE `onboarding_router`, otherwise
`/v1/pharmacies/me/...` would be captured by `/v1/pharmacies/{pharmacy_id}`.
"""
import ipaddress
import json
import re
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rova.auth.phone import InvalidPhone, normalise_mz_phone
from rova.auth.principal import Principal, require_roles
from rova.auth.router import _open_session
from rova.auth.security import hash_password
from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.storage import full_path, save_bytes
from rova.domain.enums import RoleCode
from rova.domain.machines.registry import MACHINES
from rova.notifications.router import emit
from rova.onboarding.router import _normalise_legal_name

router = APIRouter(prefix="/v1", tags=["signup"])

_REVIEWERS = (RoleCode.PLATFORM_ADMIN, RoleCode.OPS_REVIEWER, RoleCode.COMPLIANCE_OFFICER)
_PHARMACY_ANY = (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER, RoleCode.PHARMACY_RECEIVER)
_SIGNUP_ROLES = ["PharmacyAdmin", "PharmacyBuyer", "PharmacyReceiver"]

_CITIES = {
    "MAPUTO": ("MAPUTO_CIDADE", "Maputo"),
    "MATOLA": ("MATOLA", "Matola"),
    "OUTRA": ("OUTRA", "Outra região"),
}
_LICENCE_TYPE = {  # pharmacy_account.licence_type -> licence.type
    "A": "RETAIL_A", "B": "RETAIL_B", "C": "RETAIL_C",
    "POSTO_DE_VENDA": "POSTO_DE_VENDA", "HEALTH_UNIT": "RETAIL_A",
}
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG", "image/png", ".png"),
    (b"%PDF", "application/pdf", ".pdf"),
)


# ------------------------------------------------------------------ helpers

def _collapse(s: str | None) -> str:
    return " ".join((s or "").split())


def _invalid(field: str, reason: str, message: str = "invalid sign-up") -> ApiError:
    return ApiError("VALIDATION_ERROR", message, details=[{"field": field, "reason": reason}])


def _client_ip(request: Request) -> str:
    """The caller's address, for the per-IP limit.

    `X-Forwarded-For` is honoured only when the direct peer is a proxy we
    run (a private, loopback or non-IP address — Traefik on the Docker
    network, or the test client). Then the RIGHT-MOST entry is the one our
    proxy appended — the address it actually talked to — which the caller
    cannot forge; everything left of it is whatever the caller typed and is
    ignored, private or public. A request that reaches the published port
    directly is judged by its peer address alone, so a forged header buys
    nothing there either."""
    peer = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for", "")
    try:
        peer_ip = ipaddress.ip_address(peer)
        trusted = peer_ip.is_private or peer_ip.is_loopback
    except ValueError:
        trusted = True   # not an address at all (the test client says "testclient")
    if trusted and xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            try:
                ipaddress.ip_address(hops[-1])
                return hops[-1]
            except ValueError:
                pass   # not an address: fall back to the peer, never to the caller's text
    return peer or "unknown"


def _record_attempt(session: Session, phone: str, ip: str, outcome: str, *, commit: bool) -> None:
    session.execute(
        text("INSERT INTO signup_attempt (id, phone, ip, outcome, created_at) VALUES (:id, :p, :ip, :o, :t)"),
        {"id": new_id("sga"), "p": phone[:64], "ip": ip[:64], "o": outcome, "t": now()},
    )
    if commit:
        # get_session() rolls back on the error that follows; the attempt
        # has to outlive it or the limiter never sees it.
        session.commit()


async def _read_licence_file(session: Session, upload: UploadFile | None) -> tuple[bytes, str, str] | None:
    """(bytes, media type, extension) of a valid licence file, None when no
    file was sent, 422 naming `licence_file` otherwise."""
    if upload is None or (not upload.filename and not upload.size):
        return None
    max_mb = int(cfg.get(session, "CFG-LICENCE-UPLOAD-MAX-MB", default=10))
    data = await upload.read(max_mb * 1024 * 1024 + 1)
    if not data:
        raise _invalid("licence_file", "empty", "the licence file is empty")
    if len(data) > max_mb * 1024 * 1024:
        raise _invalid("licence_file", f"larger than {max_mb} MB", "the licence file is too large")
    for magic, media, ext in _MAGIC:
        if data.startswith(magic):
            return data, media, ext
    raise _invalid("licence_file", "unsupported_type", "the licence file must be a JPG, PNG or PDF")


def _sniff(path) -> str:
    with open(path, "rb") as f:
        head = f.read(8)
    for magic, media, _ in _MAGIC:
        if head.startswith(magic):
            return media
    return "application/octet-stream"


def _latest_licence(session: Session, pharmacy_id: str) -> dict | None:
    row = session.execute(
        text("SELECT * FROM licence WHERE holder_type='PHARMACY' AND holder_id=:p "
             "ORDER BY created_at DESC, id DESC LIMIT 1"), {"p": pharmacy_id},
    ).mappings().first()
    return dict(row) if row else None


def _pharmacy_or_404(session: Session, pharmacy_id: str) -> dict:
    row = session.execute(text("SELECT * FROM pharmacy_account WHERE id=:id"), {"id": pharmacy_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "pharmacy not found")
    return dict(row)


def _audit(session: Session, *, user_id: str, role: str, action: str, subject_type: str,
           subject_id: str, metadata: dict) -> None:
    session.execute(
        text("INSERT INTO audit_event (id, actor_user_id, actor_role, action_code, subject_type, "
             "subject_id, rule_ref, metadata, occurred_at) "
             "VALUES (:id, :u, :ar, :a, :st, :si, NULL, CAST(:m AS JSONB), :t)"),
        {"id": new_id("aud"), "u": user_id, "ar": role, "a": action, "st": subject_type,
         "si": subject_id, "m": json.dumps(metadata), "t": now()},
    )


def _reviewer_role(principal: Principal) -> str:
    for r in (RoleCode.COMPLIANCE_OFFICER, RoleCode.PLATFORM_ADMIN, RoleCode.OPS_REVIEWER):
        if r in principal.roles:
            return r.value
    return next(iter(sorted(principal.roles)), "UNKNOWN")


def _notify_reviewers(session: Session, payload: dict) -> None:
    for role in ("PlatformAdmin", "ComplianceOfficer", "OpsReviewer"):
        emit(session, event_code="N-PHARMACY-SIGNUP", recipient_role=role, payload=payload)


def _review_status(session: Session, pharmacy_id: str) -> dict:
    ph = _pharmacy_or_404(session, pharmacy_id)
    lic = _latest_licence(session, pharmacy_id)
    rejection_reason = decided_at = None
    if ph["status"] == "REJECTED":
        case = session.execute(
            text("SELECT decision_notes, decided_at FROM verification_case WHERE organisation_id=:o "
                 "AND decision='REJECTED' ORDER BY decided_at DESC LIMIT 1"),
            {"o": ph["organisation_id"]},
        ).mappings().first()
        if case:
            rejection_reason, decided_at = case["decision_notes"], case["decided_at"]
    elif ph["status"] in ("ACTIVE", "LICENCE_EXPIRING"):
        decided_at = session.execute(
            text("SELECT decided_at FROM verification_case WHERE organisation_id=:o "
                 "AND decision='APPROVED' ORDER BY decided_at DESC LIMIT 1"),
            {"o": ph["organisation_id"]},
        ).scalar()
    return {
        "pharmacy_id": ph["id"],
        "pharmacy_status": ph["status"],
        "can_checkout": ph["status"] in ("ACTIVE", "LICENCE_EXPIRING"),
        "licence": ({"id": lic["id"], "status": lic["status"], "has_document": bool(lic["document_ref"]),
                     "submitted_at": lic["created_at"]} if lic else None),
        "rejection_reason": rejection_reason,
        "decided_at": decided_at,
    }


def _review_item(session: Session, pharmacy_id: str) -> dict:
    ph = _pharmacy_or_404(session, pharmacy_id)
    org = session.execute(text("SELECT * FROM organisation WHERE id=:o"),
                          {"o": ph["organisation_id"]}).mappings().one()
    owner = session.execute(
        text("SELECT u.id, u.name, u.phone FROM membership m JOIN app_user u ON u.id = m.user_id "
             "WHERE m.organisation_id=:o AND m.status='ACTIVE' AND 'PharmacyAdmin' = ANY(m.role_codes) "
             "ORDER BY m.created_at ASC, m.id ASC LIMIT 1"), {"o": org["id"]},
    ).mappings().first()
    lic = _latest_licence(session, pharmacy_id)
    case_id = session.execute(
        text("SELECT id FROM verification_case WHERE organisation_id=:o AND decision='PENDING' "
             "ORDER BY opened_at DESC LIMIT 1"), {"o": org["id"]},
    ).scalar()
    same_name = session.execute(
        text("SELECT count(*) FROM organisation WHERE legal_name_normalised=:n AND id<>:o"),
        {"n": org["legal_name_normalised"], "o": org["id"]},
    ).scalar()
    return {
        "pharmacy_id": ph["id"], "trade_name": ph["trade_name"], "region_code": ph["region_code"],
        "address": ph["address"], "status": ph["status"], "organisation_id": org["id"],
        "nuit": org["tax_id"], "created_at": ph["created_at"],
        "owner": ({"user_id": owner["id"], "name": owner["name"], "phone": owner["phone"]} if owner else None),
        "licence": ({"id": lic["id"], "status": lic["status"], "has_document": bool(lic["document_ref"]),
                     "number": lic["number"], "issue_date": lic["issue_date"],
                     "expiry_date": lic["expiry_date"], "type": lic["type"]} if lic else None),
        "case_id": case_id,
        "same_name_pharmacies": int(same_name or 0),
    }


# ------------------------------------------------------------------ §2.1 sign-up

@router.post("/auth/signup", status_code=201)
async def signup(
    request: Request,
    pharmacy_name: str = Form(default=""),
    owner_name: str = Form(default=""),
    phone: str = Form(default=""),
    password: str = Form(default=""),
    city: str = Form(default=""),
    neighbourhood: str = Form(default=""),
    nuit: str = Form(default=""),
    locale: str = Form(default="pt"),
    accept_terms: str = Form(default=""),
    licence_file: UploadFile | None = File(default=None),
    session: Session = Depends(get_session, scope="function"),
):
    """Public. One transaction creates organisation, pharmacy (ONBOARDING),
    licence (SUBMITTED), user, membership, verification case, consent and
    audit, notifies the three reviewer roles, and signs the new user in.

    Every field is read as a plain form string and checked here rather than
    by FastAPI, so that a malformed attempt is still an attempt: it is
    recorded and it counts against the limits."""
    ip = _client_ip(request)

    # the per-IP budget is checked before anything else, so a stream of
    # garbage phone numbers from one address is limited like anything else
    max_ip = int(cfg.get(session, "CFG-SIGNUP-MAX-PER-IP-PER-HOUR", default=10))
    max_phone = int(cfg.get(session, "CFG-SIGNUP-MAX-PER-PHONE-PER-DAY", default=3))
    t = now()

    def _limited(p: str) -> ApiError:
        _record_attempt(session, p, ip, "RATE_LIMITED", commit=True)
        return ApiError("RATE_LIMITED", "too many sign-up attempts, try again later",
                        details=[{"field": "retry_after_seconds", "reason": "3600"}])

    by_ip = session.execute(
        text("SELECT count(*) FROM signup_attempt WHERE ip=:ip AND created_at > :since"),
        {"ip": ip, "since": t - timedelta(hours=1)},
    ).scalar()
    if by_ip >= max_ip:
        raise _limited(_collapse(phone) or "-")

    try:
        e164 = normalise_mz_phone(phone)
    except InvalidPhone:
        _record_attempt(session, _collapse(phone) or "-", ip, "INVALID", commit=True)
        raise _invalid("phone", "invalid_mz_mobile", "phone must be a Mozambican mobile number")

    by_phone = session.execute(
        text("SELECT count(*) FROM signup_attempt WHERE phone=:p AND created_at > :since"),
        {"p": e164, "since": t - timedelta(hours=24)},
    ).scalar()
    if by_phone >= max_phone:
        raise _limited(e164)

    name = _collapse(pharmacy_name)
    owner = _collapse(owner_name)
    bairro = _collapse(neighbourhood)
    tax = re.sub(r"\s", "", nuit or "")
    problem = None
    if not 2 <= len(name) <= 120:
        problem = ("pharmacy_name", "2 to 120 characters")
    elif not 2 <= len(owner) <= 120:
        problem = ("owner_name", "2 to 120 characters")
    elif not 8 <= len(password or "") <= 128:
        problem = ("password", "8 to 128 characters")
    elif (city or "").upper() not in _CITIES:
        problem = ("city", "one of MAPUTO, MATOLA, OUTRA")
    elif len(bairro) > 120:
        problem = ("neighbourhood", "at most 120 characters")
    elif tax and not re.fullmatch(r"\d{9}", tax):
        problem = ("nuit", "9 digits")
    elif (locale or "pt") not in ("pt", "ar", "en"):
        problem = ("locale", "one of pt, ar, en")
    elif (accept_terms or "").strip().lower() not in ("true", "1", "on", "yes"):
        problem = ("accept_terms", "the terms must be accepted")
    if problem:
        _record_attempt(session, e164, ip, "INVALID", commit=True)
        raise _invalid(*problem)

    if session.execute(text("SELECT 1 FROM app_user WHERE phone=:p"), {"p": e164}).first():
        _record_attempt(session, e164, ip, "DUPLICATE_PHONE", commit=True)
        raise ApiError("PHONE_ALREADY_REGISTERED", "this number already has an account — sign in instead",
                       details=[{"field": "phone", "reason": "already_registered"}])

    try:
        upload = await _read_licence_file(session, licence_file)
    except ApiError:
        _record_attempt(session, e164, ip, "INVALID", commit=True)
        raise

    region_code, city_label = _CITIES[city.upper()]
    org_id, pha_id, lic_id = new_id("org"), new_id("pha"), new_id("lic")
    usr_id, mem_id, vcs_id = new_id("usr"), new_id("mem"), new_id("vcs")

    # A NUIT some other organisation already carries (uq_organisation_tax)
    # must not turn into a 500 — and must not turn into "this NUIT is
    # taken" either, which is a fact sign-up is not allowed to reveal (§2.1,
    # §3). The account is created without it; the reviewer sees "NUIT not
    # given" and the typed value in the audit row, and decides.
    nuit_conflict = None
    if tax and session.execute(text("SELECT 1 FROM organisation WHERE country='MZ' AND tax_id=:t"),
                               {"t": tax}).first():
        nuit_conflict, tax = tax, ""

    # The user row goes first: it is the one that can collide. The duplicate
    # check above and this insert are not one step — two requests with the
    # same number can pass the check together, and the second insert then
    # trips uq_app_user_phone. That is the same answer as a duplicate, not a
    # 500: the request's rows are rolled back, the attempt is recorded, and
    # the 409 follows. Nothing has been written to disk yet at this point.
    try:
        session.execute(
            text("INSERT INTO app_user (id, phone, name, locale, password_hash, failed_login_count) "
                 "VALUES (:id, :p, :n, :l, :h, 0)"),
            {"id": usr_id, "p": e164, "n": owner, "l": "ar" if locale == "ar" else "pt",
             "h": hash_password(password)},
        )
        session.flush()
    except IntegrityError:
        session.rollback()
        _record_attempt(session, e164, ip, "DUPLICATE_PHONE", commit=True)
        raise ApiError("PHONE_ALREADY_REGISTERED", "this number already has an account — sign in instead",
                       details=[{"field": "phone", "reason": "already_registered"}])

    document_ref = save_bytes(f"licences/{pha_id}", f"alvara{upload[2]}", upload[0]) if upload else None

    session.execute(
        text("INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type, country, status) "
             "VALUES (:id, :t, :n, :nn, 'PHARMACY', 'MZ', 'ACTIVE')"),
        {"id": org_id, "t": tax or None, "n": name, "nn": _normalise_legal_name(name)},
    )
    session.execute(
        text("INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
             "latitude, longitude, status, auto_reroute, is_assisted) "
             "VALUES (:id, :o, :r, 'A', :tn, :ad, NULL, NULL, 'ONBOARDING', false, false)"),
        {"id": pha_id, "o": org_id, "r": region_code, "tn": name,
         "ad": f"{bairro}, {city_label}" if bairro else city_label},
    )
    session.execute(
        text("INSERT INTO licence (id, holder_type, holder_id, type, number, issuer, issue_date, expiry_date, "
             "document_ref, status) VALUES (:id, 'PHARMACY', :h, 'RETAIL_A', NULL, 'ANARME', NULL, NULL, "
             ":d, 'SUBMITTED')"),
        {"id": lic_id, "h": pha_id, "d": document_ref},
    )
    session.execute(
        text("INSERT INTO membership (id, user_id, organisation_id, role_codes, status) "
             "VALUES (:id, :u, :o, :r, 'ACTIVE')"),
        {"id": mem_id, "u": usr_id, "o": org_id, "r": _SIGNUP_ROLES},
    )
    session.execute(
        text("INSERT INTO verification_case (id, subject_type, organisation_id, licence_id, decision, opened_at) "
             "VALUES (:id, 'PHARMACY_ONBOARDING', :o, :l, 'PENDING', :t)"),
        {"id": vcs_id, "o": org_id, "l": lic_id, "t": now()},
    )
    terms_id = session.execute(
        text("SELECT id FROM terms_version WHERE code='PHARMACY_TERMS' ORDER BY version DESC LIMIT 1")
    ).scalar()
    if terms_id:
        session.execute(
            text("INSERT INTO consent_record (id, subject_type, subject_id, purpose, terms_version_id, "
                 "granted_by_user_id, channel_ref, granted_at) "
                 "VALUES (:id, 'USER', :u, 'TERMS_ACCEPTANCE', :tv, :u, 'APP_SIGNUP', :t)"),
            {"id": new_id("cns"), "u": usr_id, "tv": terms_id, "t": now()},
        )
    _audit(session, user_id=usr_id, role="PharmacyAdmin", action="ROLE_MEMBERSHIP_CHANGE",
           subject_type="organisation", subject_id=org_id,
           metadata={"signup": True, "pharmacy_id": pha_id, "licence_id": lic_id,
                     **({"nuit_conflict": nuit_conflict} if nuit_conflict else {})})
    _record_attempt(session, e164, ip, "CREATED", commit=False)
    _notify_reviewers(session, {"pharmacy_id": pha_id, "trade_name": name, "phone": e164,
                                "region_code": region_code, "has_document": bool(document_ref)})

    membership = session.execute(text("SELECT * FROM membership WHERE id=:m"), {"m": mem_id}).mappings().one()
    tokens = _open_session(session, usr_id, [membership], membership, "PH")
    return {
        **tokens,
        "pharmacy": {"id": pha_id, "status": "ONBOARDING", "trade_name": name},
        "licence": {"id": lic_id, "status": "SUBMITTED", "has_document": bool(document_ref)},
    }


# ------------------------------------------------------------------ §2.2 / §2.3 the pharmacy's own view

def _own_pharmacy(principal: Principal) -> str:
    if not principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "no pharmacy on this membership")
    return principal.pharmacy_id


@router.get("/pharmacies/me/review-status")
def review_status(principal: Principal = Depends(require_roles(*_PHARMACY_ANY)),
                  session: Session = Depends(get_session, scope="function")):
    return _review_status(session, _own_pharmacy(principal))


@router.post("/pharmacies/me/licence")
async def upload_own_licence(licence_file: UploadFile | None = File(default=None),
                             principal: Principal = Depends(require_roles(RoleCode.PHARMACY_ADMIN)),
                             session: Session = Depends(get_session, scope="function")):
    """Send (or send again) the Alvará: first upload from a browser when the
    phone could not attach one, or a new one after a rejection. Renewals of
    an approved pharmacy are the compliance flow, not this button."""
    pharmacy_id = _own_pharmacy(principal)
    ph = _pharmacy_or_404(session, pharmacy_id)
    if ph["status"] not in ("ONBOARDING", "REJECTED"):
        raise ApiError("GUARD_FAILED", f"pharmacy is {ph['status']}; a licence is sent here only while "
                       "the account is being checked or after a rejection")
    upload = await _read_licence_file(session, licence_file)
    if upload is None:
        raise _invalid("licence_file", "required", "choose the licence file")
    document_ref = save_bytes(f"licences/{pharmacy_id}", f"alvara{upload[2]}", upload[0])

    latest = _latest_licence(session, pharmacy_id)
    resubmission = False
    if latest and latest["status"] in ("SUBMITTED", "UNDER_REVIEW") and not latest["document_ref"]:
        session.execute(text("UPDATE licence SET document_ref=:d, updated_at=:t WHERE id=:id"),
                        {"d": document_ref, "t": now(), "id": latest["id"]})
        licence_id = latest["id"]
    else:
        resubmission = True
        licence_id = new_id("lic")
        session.execute(
            text("INSERT INTO licence (id, holder_type, holder_id, type, number, issuer, issue_date, "
                 "expiry_date, document_ref, status) VALUES (:id, 'PHARMACY', :h, :ty, NULL, 'ANARME', "
                 "NULL, NULL, :d, 'SUBMITTED')"),
            {"id": licence_id, "h": pharmacy_id, "ty": latest["type"] if latest else "RETAIL_A",
             "d": document_ref},
        )
        if ph["status"] == "REJECTED":
            MACHINES["SM-07"].apply(session, pharmacy_id, "RESUBMIT", principal)
        # one open case per pharmacy in the queue: a second photo sent while
        # the first is still waiting joins that case rather than queueing
        # the pharmacy twice.
        pending = session.execute(
            text("SELECT id FROM verification_case WHERE organisation_id=:o AND decision='PENDING'"),
            {"o": ph["organisation_id"]},
        ).scalar()
        if pending:
            session.execute(text("UPDATE verification_case SET licence_id=:l, updated_at=:t WHERE id=:id"),
                            {"l": licence_id, "t": now(), "id": pending})
        else:
            session.execute(
                text("INSERT INTO verification_case (id, subject_type, organisation_id, licence_id, decision, "
                     "opened_at) VALUES (:id, 'PHARMACY_ONBOARDING', :o, :l, 'PENDING', :t)"),
                {"id": new_id("vcs"), "o": ph["organisation_id"], "l": licence_id, "t": now()},
            )
    owner_phone = session.execute(text("SELECT phone FROM app_user WHERE id=:u"),
                                  {"u": principal.user_id}).scalar()
    _notify_reviewers(session, {"pharmacy_id": pharmacy_id, "trade_name": ph["trade_name"],
                                "phone": owner_phone, "region_code": ph["region_code"],
                                "has_document": True, "resubmission": resubmission})
    return _review_status(session, pharmacy_id)


# ------------------------------------------------------------------ §2.4 the document itself

@router.get("/licences/{licence_id}/document")
def licence_document(licence_id: str,
                     principal: Principal = Depends(require_roles(*_REVIEWERS, RoleCode.PHARMACY_ADMIN)),
                     session: Session = Depends(get_session, scope="function")):
    lic = session.execute(text("SELECT * FROM licence WHERE id=:id"), {"id": licence_id}).mappings().first()
    if lic is None:
        raise ApiError("NOT_FOUND", "licence not found")
    is_reviewer = bool(principal.roles & set(_REVIEWERS))
    if not is_reviewer and not (lic["holder_type"] == "PHARMACY" and lic["holder_id"] == principal.pharmacy_id):
        raise ApiError("NOT_FOUND", "licence not found")
    if not lic["document_ref"]:
        raise ApiError("NOT_FOUND", "no document for this licence")
    path = full_path(lic["document_ref"])
    if not path.is_file():
        raise ApiError("NOT_FOUND", "document file is missing")
    return FileResponse(path, media_type=_sniff(path),
                        headers={"Content-Disposition": "inline", "Cache-Control": "private, no-store"})


# ------------------------------------------------------------------ §2.5 / §2.6 review

@router.get("/onboarding-review/pharmacies")
def review_list(status: Literal["ONBOARDING", "REJECTED", "ALL"] = "ONBOARDING",
                limit: int = Query(default=50, ge=1, le=200),
                principal: Principal = Depends(require_roles(*_REVIEWERS)),
                session: Session = Depends(get_session, scope="function")):
    ids = session.execute(
        text("SELECT id FROM pharmacy_account WHERE (:st = 'ALL' OR status = :st) "
             "ORDER BY created_at ASC, id ASC LIMIT :lim"),
        {"st": status, "lim": limit},
    ).scalars().all()
    return {"items": [_review_item(session, i) for i in ids], "next_cursor": None}


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    licence_number: str = Field(min_length=1, max_length=64)
    issue_date: date
    expiry_date: date
    licence_type: Literal["A", "B", "C", "POSTO_DE_VENDA", "HEALTH_UNIT"] | None = None
    notes: str | None = Field(default=None, max_length=500)


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=500)


def _onboarding_or_guard(session: Session, pharmacy_id: str) -> dict:
    ph = _pharmacy_or_404(session, pharmacy_id)
    if ph["status"] != "ONBOARDING":
        raise ApiError("GUARD_FAILED", f"pharmacy is {ph['status']}, not ONBOARDING")
    return ph


def _decide_case(session: Session, organisation_id: str, decision: str, notes: str | None, reviewer: str) -> None:
    session.execute(
        text("UPDATE verification_case SET decision=:d, decision_notes=:n, reviewer_user_id=:u, "
             "decided_at=:t, updated_at=:t WHERE organisation_id=:o AND decision='PENDING' "
             "AND subject_type='PHARMACY_ONBOARDING'"),
        {"d": decision, "n": notes, "u": reviewer, "t": now(), "o": organisation_id},
    )


@router.post("/onboarding-review/pharmacies/{pharmacy_id}/approve")
def review_approve(pharmacy_id: str, body: ApproveBody,
                   principal: Principal = Depends(require_roles(*_REVIEWERS)),
                   session: Session = Depends(get_session, scope="function")):
    ph = _onboarding_or_guard(session, pharmacy_id)
    lic = _latest_licence(session, pharmacy_id)
    if lic is None:
        raise ApiError("GUARD_FAILED", "pharmacy has no licence", rule="R-113")
    if not lic["document_ref"]:
        raise ApiError("GUARD_FAILED", "licence has no document", rule="R-113",
                       details=[{"field": "licence", "reason": "licence has no document"}])
    if body.expiry_date <= body.issue_date:
        raise _invalid("expiry_date", "must be after issue_date", "expiry_date must be after issue_date")
    if body.expiry_date <= now().date():
        raise _invalid("expiry_date", "must be in the future", "the licence has already expired")

    lic_type = _LICENCE_TYPE[body.licence_type] if body.licence_type else lic["type"]
    session.execute(
        text("UPDATE licence SET number=:n, issue_date=:i, expiry_date=:e, type=:ty, updated_at=:t WHERE id=:id"),
        {"n": body.licence_number.strip(), "i": body.issue_date, "e": body.expiry_date, "ty": lic_type,
         "t": now(), "id": lic["id"]},
    )
    if body.licence_type:
        session.execute(text("UPDATE pharmacy_account SET licence_type=:lt, updated_at=:t WHERE id=:id"),
                        {"lt": body.licence_type, "t": now(), "id": pharmacy_id})
    if lic["status"] == "SUBMITTED":
        MACHINES["SM-08"].apply(session, lic["id"], "OPEN_REVIEW", principal)
    # SM-08 APPROVE's effect fires SM-07 APPROVE with this same principal;
    # SM-07's R-113 guard then reads the licence this call just made VALID.
    MACHINES["SM-08"].apply(session, lic["id"], "APPROVE", principal)
    if _pharmacy_or_404(session, pharmacy_id)["status"] != "ACTIVE":
        raise ApiError("GUARD_FAILED", "pharmacy did not become ACTIVE", rule="R-113")

    _decide_case(session, ph["organisation_id"], "APPROVED", body.notes, principal.user_id)
    _audit(session, user_id=principal.user_id, role=_reviewer_role(principal), action="LICENCE_DECISION",
           subject_type="pharmacy_account", subject_id=pharmacy_id,
           metadata={"decision": "APPROVED", "licence_id": lic["id"]})
    emit(session, event_code="N-PHARMACY-APPROVED", recipient_org_id=ph["organisation_id"],
         payload={"pharmacy_id": pharmacy_id, "trade_name": ph["trade_name"]})
    return _review_item(session, pharmacy_id)


@router.post("/onboarding-review/pharmacies/{pharmacy_id}/reject")
def review_reject(pharmacy_id: str, body: RejectBody,
                  principal: Principal = Depends(require_roles(*_REVIEWERS)),
                  session: Session = Depends(get_session, scope="function")):
    ph = _onboarding_or_guard(session, pharmacy_id)
    reason = body.reason.strip()
    if len(reason) < 3:
        raise _invalid("reason", "at least 3 characters")
    lic = _latest_licence(session, pharmacy_id)
    if lic and lic["status"] == "SUBMITTED":
        MACHINES["SM-08"].apply(session, lic["id"], "OPEN_REVIEW", principal)
        lic["status"] = "UNDER_REVIEW"
    if lic and lic["status"] == "UNDER_REVIEW":
        MACHINES["SM-08"].apply(session, lic["id"], "REJECT", principal)
    MACHINES["SM-07"].apply(session, pharmacy_id, "REJECT_VERIFICATION", principal)
    _decide_case(session, ph["organisation_id"], "REJECTED", reason, principal.user_id)
    _audit(session, user_id=principal.user_id, role=_reviewer_role(principal), action="LICENCE_DECISION",
           subject_type="pharmacy_account", subject_id=pharmacy_id,
           metadata={"decision": "REJECTED", "reason": reason, "licence_id": lic["id"] if lic else None})
    emit(session, event_code="N-PHARMACY-REJECTED", recipient_org_id=ph["organisation_id"],
         payload={"pharmacy_id": pharmacy_id, "reason": reason})
    return _review_item(session, pharmacy_id)


@router.post("/onboarding-review/pharmacies/{pharmacy_id}/licence-document")
async def review_attach_document(pharmacy_id: str, licence_file: UploadFile | None = File(default=None),
                                 principal: Principal = Depends(require_roles(*_REVIEWERS)),
                                 session: Session = Depends(get_session, scope="function")):
    """The reviewer attaches the Alvará received by WhatsApp or e-mail on the
    pharmacy's behalf. Never a silent replace: a licence that already has a
    document is 409 — reject and let the pharmacy send a new one."""
    _pharmacy_or_404(session, pharmacy_id)
    lic = _latest_licence(session, pharmacy_id)
    if lic is None or lic["status"] not in ("SUBMITTED", "UNDER_REVIEW"):
        raise ApiError("GUARD_FAILED", "no licence waiting for review")
    if lic["document_ref"]:
        raise ApiError("CONFLICT", "this licence already has a document; reject it and ask for a new one")
    upload = await _read_licence_file(session, licence_file)
    if upload is None:
        raise _invalid("licence_file", "required", "choose the licence file")
    document_ref = save_bytes(f"licences/{pharmacy_id}", f"alvara{upload[2]}", upload[0])
    session.execute(text("UPDATE licence SET document_ref=:d, updated_at=:t WHERE id=:id"),
                    {"d": document_ref, "t": now(), "id": lic["id"]})
    _audit(session, user_id=principal.user_id, role=_reviewer_role(principal), action="LICENCE_DECISION",
           subject_type="licence", subject_id=lic["id"],
           metadata={"document_attached_by_reviewer": True, "pharmacy_id": pharmacy_id})
    return _review_item(session, pharmacy_id)
