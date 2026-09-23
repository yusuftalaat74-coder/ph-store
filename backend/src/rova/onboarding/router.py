"""Onboarding surface — organisation, pharmacy, vendor, licence, verification
case, terms and membership.

The state machines SM-06 (vendor_account), SM-07 (pharmacy_account) and SM-08
(licence) already existed and were fully tested; nothing in this module
re-implements a transition. Every state change here goes through
`MACHINES[...].apply(...)`, so the append-only `state_transition` log and the
guards (R-107 / R-111 / R-112 / R-143) hold exactly as they did before this
surface existed.

Enum values accepted by the request models are `Literal`s copied from the
CHECK constraints in `migrations/sql/0001_initial.sql`, so a wrong value is a
422 naming the field — never a 500 from a CHECK violation.
"""
import json
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.auth.security import hash_password
from rova.core.clock import now
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain.enums import RoleCode
from rova.domain.machines.registry import MACHINES

router = APIRouter(prefix="/v1", tags=["onboarding"])

_COMPLIANCE = (RoleCode.COMPLIANCE_OFFICER,)
_COMPLIANCE_ADMIN = (RoleCode.COMPLIANCE_OFFICER, RoleCode.PLATFORM_ADMIN)
_PLATFORM_ANY = (RoleCode.COMPLIANCE_OFFICER, RoleCode.PLATFORM_ADMIN, RoleCode.OPS_REVIEWER)
_VENDOR_ADMIN = (RoleCode.VENDOR_ADMIN,)
_PHARMACY_ADMIN = (RoleCode.PHARMACY_ADMIN,)

OrgType = Literal["PHARMACY", "VENDOR", "TRANSPORTER"]
LicenceType = Literal["WHOLESALE_ALVARA", "RETAIL_A", "RETAIL_B", "RETAIL_C",
                      "POSTO_DE_VENDA", "TRANSPORTER_LICENCE"]
HolderType = Literal["VENDOR", "PHARMACY", "TRANSPORTER"]
PharmacyLicenceType = Literal["A", "B", "C", "POSTO_DE_VENDA", "HEALTH_UNIT"]
VendorType = Literal["IMPORTER_WHOLESALER", "DISTRIBUTOR"]
DeliveryMode = Literal["VENDOR_OWN_FLEET", "LICENSED_TRANSPORTER", "PLATFORM_COORDINATED_COURIER"]
AcceptanceMode = Literal["MANUAL_CONFIRM", "AUTO_ACCEPT_FULL"]
AllocationStrategy = Literal["FEWEST_VENDORS", "FASTEST_DISPATCH", "BEST_TERMS"]
VerificationSubject = Literal["VENDOR_ONBOARDING", "PHARMACY_ONBOARDING", "LICENCE_RENEWAL", "CONFLICT_CHECK"]
VerificationDecision = Literal["APPROVED", "REJECTED", "RESUBMISSION_REQUESTED"]
TermsCodeLit = Literal["PHARMACY_TERMS", "VENDOR_TERMS", "PMS_LINK_TERMS"]
Locale = Literal["pt", "ar"]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _normalise_legal_name(name: str) -> str:
    return " ".join(name.lower().split())


def _row_or_404(session: Session, table: str, row_id: str, label: str) -> dict:
    row = session.execute(text(f'SELECT * FROM "{table}" WHERE id=:id'), {"id": row_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", f"{label} not found")
    return dict(row)


def _audit(session: Session, principal: Principal, *, action: str, subject_type: str, subject_id: str,
           rule_ref: str | None = None, metadata: str = "{}") -> None:
    session.execute(
        text("INSERT INTO audit_event (id, actor_user_id, actor_role, action_code, subject_type, "
             "subject_id, rule_ref, metadata, occurred_at) "
             "VALUES (:id, :u, :ar, :a, :st, :si, :rr, CAST(:m AS JSONB), :t)"),
        {"id": new_id("aud"), "u": principal.user_id,
         "ar": next(iter(sorted(principal.roles)), "UNKNOWN"), "a": action,
         "st": subject_type, "si": subject_id, "rr": rule_ref, "m": metadata, "t": now()},
    )


# ----------------------------------------------------------------- organisation

class CreateOrganisation(Body):
    tax_id: str = Field(min_length=1, max_length=64)
    legal_name: str = Field(min_length=1, max_length=255)
    type: OrgType
    country: str = Field(default="MZ", min_length=2, max_length=2)


@router.post("/organisations", status_code=201)
def create_organisation(body: CreateOrganisation,
                        principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
                        session: Session = Depends(get_session, scope="function")):
    existing = session.execute(text("SELECT id FROM organisation WHERE tax_id=:t"), {"t": body.tax_id}).scalar()
    if existing:
        raise ApiError("CONFLICT", "an organisation with this tax_id already exists")
    org_id = new_id("org")
    session.execute(
        text("INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type, country, status) "
             "VALUES (:id, :t, :n, :nn, :ty, :c, 'ACTIVE')"),
        {"id": org_id, "t": body.tax_id, "n": body.legal_name,
         "nn": _normalise_legal_name(body.legal_name), "ty": body.type, "c": body.country},
    )
    _audit(session, principal, action="ROLE_MEMBERSHIP_CHANGE",
           subject_type="organisation", subject_id=org_id)
    return _row_or_404(session, "organisation", org_id, "organisation")


@router.get("/organisations")
def list_organisations(type: OrgType | None = None, status: str | None = None,
                       limit: int = Query(default=50, ge=1, le=200),
                       principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
                       session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM organisation WHERE (CAST(:ty AS TEXT) IS NULL OR type=:ty) AND (CAST(:st AS TEXT) IS NULL OR status=:st) "
             "ORDER BY created_at DESC LIMIT :lim"),
        {"ty": type, "st": status, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/organisations/{organisation_id}")
def get_organisation(organisation_id: str,
                     principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
                     session: Session = Depends(get_session, scope="function")):
    return _row_or_404(session, "organisation", organisation_id, "organisation")


# ----------------------------------------------------------------- pharmacy

class CreatePharmacy(Body):
    organisation_id: str
    region_code: str
    licence_type: PharmacyLicenceType
    trade_name: str = Field(min_length=1, max_length=255)
    address: str = Field(min_length=1)
    latitude: float
    longitude: float
    auto_reroute: bool = True
    default_allocation_strategy: AllocationStrategy | None = None


@router.post("/pharmacies", status_code=201)
def create_pharmacy(body: CreatePharmacy,
                    principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
                    session: Session = Depends(get_session, scope="function")):
    org = _row_or_404(session, "organisation", body.organisation_id, "organisation")
    if org["type"] != "PHARMACY":
        raise ApiError("VALIDATION_ERROR", "organisation.type must be PHARMACY")
    if not session.execute(text("SELECT 1 FROM region WHERE code=:c"), {"c": body.region_code}).first():
        raise ApiError("VALIDATION_ERROR", "unknown region_code")
    pid = new_id("pha")
    session.execute(
        text("INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
             "latitude, longitude, status, auto_reroute, default_allocation_strategy, is_assisted) "
             "VALUES (:id, :o, :r, :lt, :tn, :ad, :la, :lo, 'ONBOARDING', :ar, :das, false)"),
        {"id": pid, "o": body.organisation_id, "r": body.region_code, "lt": body.licence_type,
         "tn": body.trade_name, "ad": body.address, "la": body.latitude, "lo": body.longitude,
         "ar": body.auto_reroute, "das": body.default_allocation_strategy},
    )
    return _row_or_404(session, "pharmacy_account", pid, "pharmacy")


@router.get("/pharmacies")
def list_pharmacies(status: str | None = None, region_code: str | None = None,
                    limit: int = Query(default=50, ge=1, le=200),
                    principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
                    session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM pharmacy_account WHERE (CAST(:st AS TEXT) IS NULL OR status=:st) "
             "AND (CAST(:rc AS TEXT) IS NULL OR region_code=:rc) ORDER BY created_at DESC LIMIT :lim"),
        {"st": status, "rc": region_code, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/pharmacies/{pharmacy_id}")
def get_pharmacy(pharmacy_id: str,
                 principal: Principal = Depends(require_roles(
                     *(_PLATFORM_ANY + (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER)))),
                 session: Session = Depends(get_session, scope="function")):
    row = _row_or_404(session, "pharmacy_account", pharmacy_id, "pharmacy")
    if principal.pharmacy_id and principal.pharmacy_id != pharmacy_id:
        raise ApiError("NOT_FOUND", "pharmacy not found")
    return row


class PatchPharmacy(Body):
    trade_name: str | None = None
    address: str | None = None
    auto_reroute: bool | None = None
    buyer_approval_threshold: float | None = Field(default=None, gt=0)
    default_allocation_strategy: AllocationStrategy | None = None


@router.patch("/pharmacies/{pharmacy_id}")
def patch_pharmacy(pharmacy_id: str, body: PatchPharmacy,
                   principal: Principal = Depends(require_roles(RoleCode.PHARMACY_ADMIN, RoleCode.PLATFORM_ADMIN)),
                   session: Session = Depends(get_session, scope="function")):
    _row_or_404(session, "pharmacy_account", pharmacy_id, "pharmacy")
    if principal.pharmacy_id and principal.pharmacy_id != pharmacy_id:
        raise ApiError("NOT_FOUND", "pharmacy not found")
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if not fields:
        raise ApiError("VALIDATION_ERROR", "no fields to update")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    session.execute(text(f"UPDATE pharmacy_account SET {sets}, updated_at = :t WHERE id = :id"),
                    {**fields, "t": now(), "id": pharmacy_id})
    return _row_or_404(session, "pharmacy_account", pharmacy_id, "pharmacy")


def _pharmacy_transition(trigger: str, roles):
    def _endpoint(pharmacy_id: str,
                  principal: Principal = Depends(require_roles(*roles)),
                  session: Session = Depends(get_session, scope="function")):
        return dict(MACHINES["SM-07"].apply(session, pharmacy_id, trigger, principal))
    return _endpoint


router.add_api_route("/pharmacies/{pharmacy_id}/approve", _pharmacy_transition("APPROVE", _COMPLIANCE),
                     methods=["POST"], name="approve_pharmacy", tags=["onboarding"])
router.add_api_route("/pharmacies/{pharmacy_id}/reject", _pharmacy_transition("REJECT_VERIFICATION", _COMPLIANCE),
                     methods=["POST"], name="reject_pharmacy", tags=["onboarding"])
router.add_api_route("/pharmacies/{pharmacy_id}/resubmit", _pharmacy_transition("RESUBMIT", _PHARMACY_ADMIN),
                     methods=["POST"], name="resubmit_pharmacy", tags=["onboarding"])
router.add_api_route("/pharmacies/{pharmacy_id}/suspend", _pharmacy_transition("MANUAL_SUSPEND", _COMPLIANCE_ADMIN),
                     methods=["POST"], name="suspend_pharmacy", tags=["onboarding"])
router.add_api_route("/pharmacies/{pharmacy_id}/reinstate", _pharmacy_transition("REINSTATE", _COMPLIANCE_ADMIN),
                     methods=["POST"], name="reinstate_pharmacy", tags=["onboarding"])
router.add_api_route("/pharmacies/{pharmacy_id}/close", _pharmacy_transition("CLOSE", _COMPLIANCE_ADMIN),
                     methods=["POST"], name="close_pharmacy", tags=["onboarding"])


# ----------------------------------------------------------------- vendor

class CreateVendor(Body):
    organisation_id: str
    region_code: str
    trade_name: str = Field(min_length=1, max_length=255)
    vendor_type: VendorType
    delivery_mode: DeliveryMode
    mov_amount: float = Field(ge=0)
    acceptance_mode: AcceptanceMode = "MANUAL_CONFIRM"
    sourcing_attestation: bool = False
    locale: Locale = "pt"


@router.post("/vendors", status_code=201)
def create_vendor(body: CreateVendor,
                  principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
                  session: Session = Depends(get_session, scope="function")):
    org = _row_or_404(session, "organisation", body.organisation_id, "organisation")
    if org["type"] != "VENDOR":
        raise ApiError("VALIDATION_ERROR", "organisation.type must be VENDOR")
    if not session.execute(text("SELECT 1 FROM region WHERE code=:c"), {"c": body.region_code}).first():
        raise ApiError("VALIDATION_ERROR", "unknown region_code")
    vid = new_id("ven")
    session.execute(
        text("INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, "
             "delivery_mode, mov_amount, acceptance_mode, sourcing_attestation, locale, status) "
             "VALUES (:id, :o, :r, :tn, :vt, :dm, :mov, :am, :sa, :lo, 'ONBOARDING')"),
        {"id": vid, "o": body.organisation_id, "r": body.region_code, "tn": body.trade_name,
         "vt": body.vendor_type, "dm": body.delivery_mode, "mov": body.mov_amount,
         "am": body.acceptance_mode, "sa": body.sourcing_attestation, "lo": body.locale},
    )
    return _row_or_404(session, "vendor_account", vid, "vendor")


@router.get("/vendors")
def list_vendors(status: str | None = None, region_code: str | None = None,
                 limit: int = Query(default=50, ge=1, le=200),
                 principal: Principal = Depends(require_roles(
                     *(_PLATFORM_ANY + (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER)))),
                 session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM vendor_account WHERE (CAST(:st AS TEXT) IS NULL OR status=:st) "
             "AND (CAST(:rc AS TEXT) IS NULL OR region_code=:rc) ORDER BY created_at DESC LIMIT :lim"),
        {"st": status, "rc": region_code, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/vendors/{vendor_id}")
def get_vendor(vendor_id: str,
               principal: Principal = Depends(require_roles(
                   *(_PLATFORM_ANY + (RoleCode.VENDOR_ADMIN, RoleCode.VENDOR_ORDER_DESK,
                                      RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER)))),
               session: Session = Depends(get_session, scope="function")):
    row = _row_or_404(session, "vendor_account", vendor_id, "vendor")
    if principal.vendor_id and principal.vendor_id != vendor_id:
        raise ApiError("NOT_FOUND", "vendor not found")
    return row


class PatchVendor(Body):
    trade_name: str | None = None
    delivery_mode: DeliveryMode | None = None
    mov_amount: float | None = Field(default=None, ge=0)
    acceptance_mode: AcceptanceMode | None = None
    locale: Locale | None = None


@router.patch("/vendors/{vendor_id}")
def patch_vendor(vendor_id: str, body: PatchVendor,
                 principal: Principal = Depends(require_roles(RoleCode.VENDOR_ADMIN, RoleCode.PLATFORM_ADMIN)),
                 session: Session = Depends(get_session, scope="function")):
    _row_or_404(session, "vendor_account", vendor_id, "vendor")
    if principal.vendor_id and principal.vendor_id != vendor_id:
        raise ApiError("NOT_FOUND", "vendor not found")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise ApiError("VALIDATION_ERROR", "no fields to update")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    session.execute(text(f"UPDATE vendor_account SET {sets}, updated_at = :t WHERE id = :id"),
                    {**fields, "t": now(), "id": vendor_id})
    return _row_or_404(session, "vendor_account", vendor_id, "vendor")


class Attestation(Body):
    sourcing_attestation: bool


@router.post("/vendors/{vendor_id}/attestation")
def set_attestation(vendor_id: str, body: Attestation,
                    principal: Principal = Depends(require_roles(RoleCode.VENDOR_ADMIN, RoleCode.COMPLIANCE_OFFICER)),
                    session: Session = Depends(get_session, scope="function")):
    """R-112 / Art. 40 — the attestation is an audited fact, not a silent column."""
    before = _row_or_404(session, "vendor_account", vendor_id, "vendor")
    if principal.vendor_id and principal.vendor_id != vendor_id:
        raise ApiError("NOT_FOUND", "vendor not found")
    session.execute(text("UPDATE vendor_account SET sourcing_attestation=:s, updated_at=:t WHERE id=:id"),
                    {"s": body.sourcing_attestation, "t": now(), "id": vendor_id})
    _audit(session, principal, action="SOURCING_ATTESTATION_CHANGE", rule_ref="R-112",
           subject_type="vendor_account", subject_id=vendor_id,
           metadata=json.dumps({"before": bool(before["sourcing_attestation"]),
                                "after": bool(body.sourcing_attestation)}))
    return _row_or_404(session, "vendor_account", vendor_id, "vendor")


def _vendor_transition(trigger: str, roles):
    def _endpoint(vendor_id: str,
                  principal: Principal = Depends(require_roles(*roles)),
                  session: Session = Depends(get_session, scope="function")):
        return dict(MACHINES["SM-06"].apply(session, vendor_id, trigger, principal))
    return _endpoint


router.add_api_route("/vendors/{vendor_id}/approve", _vendor_transition("APPROVE", _COMPLIANCE),
                     methods=["POST"], name="approve_vendor", tags=["onboarding"])
router.add_api_route("/vendors/{vendor_id}/reject", _vendor_transition("REJECT_VERIFICATION", _COMPLIANCE),
                     methods=["POST"], name="reject_vendor", tags=["onboarding"])
router.add_api_route("/vendors/{vendor_id}/resubmit", _vendor_transition("RESUBMIT", _VENDOR_ADMIN),
                     methods=["POST"], name="resubmit_vendor", tags=["onboarding"])
router.add_api_route("/vendors/{vendor_id}/suspend", _vendor_transition("MANUAL_SUSPEND", _COMPLIANCE_ADMIN),
                     methods=["POST"], name="suspend_vendor", tags=["onboarding"])
router.add_api_route("/vendors/{vendor_id}/reinstate", _vendor_transition("REINSTATE", _COMPLIANCE_ADMIN),
                     methods=["POST"], name="reinstate_vendor", tags=["onboarding"])
router.add_api_route("/vendors/{vendor_id}/close", _vendor_transition("CLOSE", _COMPLIANCE_ADMIN),
                     methods=["POST"], name="close_vendor", tags=["onboarding"])


# ----------------------------------------------------------------- vendor agreement

class CreateAgreement(Body):
    vendor_id: str
    document_ref: str
    founding_supplier: bool = False
    multi_vendor_clause_ack: bool = False
    exclusivity_until: date | None = None
    preferential_rate_until: date | None = None
    badge_until: date | None = None


@router.post("/vendor-agreements", status_code=201)
def create_agreement(body: CreateAgreement,
                     principal: Principal = Depends(require_roles(*_COMPLIANCE_ADMIN)),
                     session: Session = Depends(get_session, scope="function")):
    _row_or_404(session, "vendor_account", body.vendor_id, "vendor")
    aid = new_id("vag")
    session.execute(
        text("INSERT INTO vendor_agreement (id, vendor_id, signed_at, document_ref, founding_supplier, "
             "multi_vendor_clause_ack, exclusivity_until, preferential_rate_until, badge_until, status) "
             "VALUES (:id, :v, :t, :d, :f, :m, :e, :p, :b, 'ACTIVE')"),
        {"id": aid, "v": body.vendor_id, "t": now(), "d": body.document_ref,
         "f": body.founding_supplier, "m": body.multi_vendor_clause_ack,
         "e": body.exclusivity_until, "p": body.preferential_rate_until, "b": body.badge_until},
    )
    return _row_or_404(session, "vendor_agreement", aid, "vendor agreement")


@router.get("/vendor-agreements")
def list_agreements(vendor_id: str | None = None,
                    principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
                    session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM vendor_agreement WHERE (CAST(:v AS TEXT) IS NULL OR vendor_id=:v) ORDER BY created_at DESC LIMIT 200"),
        {"v": vendor_id},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


# ----------------------------------------------------------------- licence

class CreateLicence(Body):
    holder_type: HolderType
    holder_id: str
    type: LicenceType
    number: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    issue_date: date
    expiry_date: date
    document_ref: str = Field(min_length=1)


@router.post("/licences", status_code=201)
def create_licence(body: CreateLicence,
                   principal: Principal = Depends(require_roles(
                       RoleCode.VENDOR_ADMIN, RoleCode.PHARMACY_ADMIN, *_COMPLIANCE_ADMIN)),
                   session: Session = Depends(get_session, scope="function")):
    if body.expiry_date <= body.issue_date:
        raise ApiError("VALIDATION_ERROR", "expiry_date must be after issue_date")
    table = {"VENDOR": "vendor_account", "PHARMACY": "pharmacy_account", "TRANSPORTER": "transporter_account"}
    _row_or_404(session, table[body.holder_type], body.holder_id, "licence holder")
    lid = new_id("lic")
    session.execute(
        text("INSERT INTO licence (id, holder_type, holder_id, type, number, issuer, issue_date, expiry_date, "
             "document_ref, status) VALUES (:id, :ht, :hi, :ty, :n, :is, :idt, :edt, :dr, 'SUBMITTED')"),
        {"id": lid, "ht": body.holder_type, "hi": body.holder_id, "ty": body.type, "n": body.number,
         "is": body.issuer, "idt": body.issue_date, "edt": body.expiry_date, "dr": body.document_ref},
    )
    return _row_or_404(session, "licence", lid, "licence")


@router.get("/licences")
def list_licences(holder_type: HolderType | None = None, holder_id: str | None = None,
                  status: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                  principal: Principal = Depends(require_roles(
                      *(_PLATFORM_ANY + (RoleCode.VENDOR_ADMIN, RoleCode.PHARMACY_ADMIN)))),
                  session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM licence WHERE (CAST(:ht AS TEXT) IS NULL OR holder_type=:ht) AND (CAST(:hi AS TEXT) IS NULL OR holder_id=:hi) "
             "AND (CAST(:st AS TEXT) IS NULL OR status=:st) ORDER BY expiry_date ASC LIMIT :lim"),
        {"ht": holder_type, "hi": holder_id, "st": status, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/licences/{licence_id}")
def get_licence(licence_id: str,
                principal: Principal = Depends(require_roles(
                    *(_PLATFORM_ANY + (RoleCode.VENDOR_ADMIN, RoleCode.PHARMACY_ADMIN)))),
                session: Session = Depends(get_session, scope="function")):
    return _row_or_404(session, "licence", licence_id, "licence")


@router.get("/licences/{licence_id}/transitions")
def licence_transitions(licence_id: str,
                        principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
                        session: Session = Depends(get_session, scope="function")):
    _row_or_404(session, "licence", licence_id, "licence")
    rows = session.execute(
        text("SELECT * FROM state_transition WHERE subject_type='licence' AND subject_id=:id "
             "ORDER BY occurred_at ASC"), {"id": licence_id},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


def _licence_transition(trigger: str, roles):
    def _endpoint(licence_id: str,
                  principal: Principal = Depends(require_roles(*roles)),
                  session: Session = Depends(get_session, scope="function")):
        return dict(MACHINES["SM-08"].apply(session, licence_id, trigger, principal))
    return _endpoint


router.add_api_route("/licences/{licence_id}/open-review", _licence_transition("OPEN_REVIEW", _COMPLIANCE),
                     methods=["POST"], name="open_licence_review", tags=["onboarding"])
router.add_api_route("/licences/{licence_id}/approve", _licence_transition("APPROVE", _COMPLIANCE),
                     methods=["POST"], name="approve_licence", tags=["onboarding"])
router.add_api_route("/licences/{licence_id}/reject", _licence_transition("REJECT", _COMPLIANCE),
                     methods=["POST"], name="reject_licence", tags=["onboarding"])
router.add_api_route("/licences/{licence_id}/renew", _licence_transition("RENEW", _COMPLIANCE),
                     methods=["POST"], name="renew_licence", tags=["onboarding"])


# ----------------------------------------------------------------- verification case

class CreateVerificationCase(Body):
    subject_type: VerificationSubject
    organisation_id: str
    licence_id: str | None = None


@router.post("/verification-cases", status_code=201)
def create_case(body: CreateVerificationCase,
                principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
                session: Session = Depends(get_session, scope="function")):
    _row_or_404(session, "organisation", body.organisation_id, "organisation")
    if body.licence_id:
        _row_or_404(session, "licence", body.licence_id, "licence")
    cid = new_id("vcs")
    session.execute(
        text("INSERT INTO verification_case (id, subject_type, organisation_id, licence_id, decision, opened_at) "
             "VALUES (:id, :st, :o, :l, 'PENDING', :t)"),
        {"id": cid, "st": body.subject_type, "o": body.organisation_id, "l": body.licence_id, "t": now()},
    )
    return _row_or_404(session, "verification_case", cid, "verification case")


@router.get("/verification-cases")
def list_cases(decision: str | None = None, subject_type: VerificationSubject | None = None,
               limit: int = Query(default=50, ge=1, le=200),
               principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
               session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM verification_case WHERE (CAST(:d AS TEXT) IS NULL OR decision=:d) "
             "AND (CAST(:st AS TEXT) IS NULL OR subject_type=:st) ORDER BY opened_at ASC LIMIT :lim"),
        {"d": decision, "st": subject_type, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/verification-cases/{case_id}")
def get_case(case_id: str,
             principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
             session: Session = Depends(get_session, scope="function")):
    return _row_or_404(session, "verification_case", case_id, "verification case")


@router.post("/verification-cases/{case_id}/assign")
def assign_case(case_id: str,
                principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
                session: Session = Depends(get_session, scope="function")):
    case = _row_or_404(session, "verification_case", case_id, "verification case")
    if case["decision"] != "PENDING":
        raise ApiError("ILLEGAL_TRANSITION", "case is already decided")
    session.execute(text("UPDATE verification_case SET reviewer_user_id=:u, updated_at=:t WHERE id=:id"),
                    {"u": principal.user_id, "t": now(), "id": case_id})
    return _row_or_404(session, "verification_case", case_id, "verification case")


class DecideCase(Body):
    decision: VerificationDecision
    decision_notes: str | None = None


@router.post("/verification-cases/{case_id}/decide")
def decide_case(case_id: str, body: DecideCase,
                principal: Principal = Depends(require_roles(*_COMPLIANCE)),
                session: Session = Depends(get_session, scope="function")):
    case = _row_or_404(session, "verification_case", case_id, "verification case")
    if case["decision"] != "PENDING":
        raise ApiError("ILLEGAL_TRANSITION", "case is already decided")
    session.execute(
        text("UPDATE verification_case SET decision=:d, decision_notes=:n, reviewer_user_id=:u, "
             "decided_at=:t, updated_at=:t WHERE id=:id AND decision='PENDING'"),
        {"d": body.decision, "n": body.decision_notes, "u": principal.user_id, "t": now(), "id": case_id},
    )
    _audit(session, principal, action="LICENCE_DECISION",
           subject_type="verification_case", subject_id=case_id,
           metadata=json.dumps({"decision": body.decision}))
    return _row_or_404(session, "verification_case", case_id, "verification case")


# ----------------------------------------------------------------- terms & consent

@router.get("/terms")
def list_terms(code: TermsCodeLit | None = None,
               principal: Principal = Depends(require_roles(*[r for r in RoleCode])),
               session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM terms_version WHERE (CAST(:c AS TEXT) IS NULL OR code=:c) ORDER BY code, version DESC"),
        {"c": code},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/terms/{code}/current")
def current_terms(code: TermsCodeLit,
                  principal: Principal = Depends(require_roles(*[r for r in RoleCode])),
                  session: Session = Depends(get_session, scope="function")):
    row = session.execute(
        text("SELECT * FROM terms_version WHERE code=:c ORDER BY version DESC LIMIT 1"), {"c": code},
    ).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "no published terms for this code")
    return dict(row)


class AcceptTerms(Body):
    terms_version_id: str
    subject_type: Literal["USER", "PHARMACY_ACCOUNT", "VENDOR_ACCOUNT"]
    subject_id: str
    purpose: str = "TERMS_ACCEPTANCE"
    channel_ref: str | None = None


@router.post("/terms/accept", status_code=201)
def accept_terms(body: AcceptTerms,
                 principal: Principal = Depends(require_roles(*[r for r in RoleCode])),
                 session: Session = Depends(get_session, scope="function")):
    _row_or_404(session, "terms_version", body.terms_version_id, "terms version")
    cid = new_id("cns")
    session.execute(
        text("INSERT INTO consent_record (id, subject_type, subject_id, purpose, terms_version_id, "
             "granted_by_user_id, channel_ref, granted_at) VALUES (:id, :st, :si, :p, :tv, :u, :ch, :t)"),
        {"id": cid, "st": body.subject_type, "si": body.subject_id, "p": body.purpose,
         "tv": body.terms_version_id, "u": principal.user_id, "ch": body.channel_ref, "t": now()},
    )
    return _row_or_404(session, "consent_record", cid, "consent record")


@router.get("/consents")
def list_consents(subject_id: str | None = None,
                  principal: Principal = Depends(require_roles(*_PLATFORM_ANY)),
                  session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM consent_record WHERE (CAST(:s AS TEXT) IS NULL OR subject_id=:s) ORDER BY granted_at DESC LIMIT 200"),
        {"s": subject_id},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.post("/consents/{consent_id}/withdraw")
def withdraw_consent(consent_id: str,
                     principal: Principal = Depends(require_roles(*[r for r in RoleCode])),
                     session: Session = Depends(get_session, scope="function")):
    row = _row_or_404(session, "consent_record", consent_id, "consent record")
    if row["withdrawn_at"] is not None:
        raise ApiError("ILLEGAL_TRANSITION", "consent already withdrawn")
    session.execute(text("UPDATE consent_record SET withdrawn_at=:t WHERE id=:id"), {"t": now(), "id": consent_id})
    _audit(session, principal, action="CONSENT_WITHDRAWN",
           subject_type="consent_record", subject_id=consent_id)
    return _row_or_404(session, "consent_record", consent_id, "consent record")


# ----------------------------------------------------------------- users & memberships

_KNOWN_ROLES = tuple(r.value for r in RoleCode)


class CreateUser(Body):
    phone: str = Field(min_length=6, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    locale: Locale = "pt"


@router.post("/users", status_code=201)
def create_user(body: CreateUser,
                principal: Principal = Depends(require_roles(RoleCode.PLATFORM_ADMIN)),
                session: Session = Depends(get_session, scope="function")):
    if session.execute(text("SELECT 1 FROM app_user WHERE phone=:p"), {"p": body.phone}).first():
        raise ApiError("CONFLICT", "a user with this phone already exists")
    uid = new_id("usr")
    session.execute(
        text("INSERT INTO app_user (id, phone, name, locale, password_hash, failed_login_count) "
             "VALUES (:id, :p, :n, :l, :h, 0)"),
        {"id": uid, "p": body.phone, "n": body.name, "l": body.locale, "h": hash_password(body.password)},
    )
    row = _row_or_404(session, "app_user", uid, "user")
    row.pop("password_hash", None)
    return row


@router.get("/users/{user_id}")
def get_user(user_id: str,
             principal: Principal = Depends(require_roles(RoleCode.PLATFORM_ADMIN, RoleCode.SUPPORT_AGENT)),
             session: Session = Depends(get_session, scope="function")):
    row = _row_or_404(session, "app_user", user_id, "user")
    row.pop("password_hash", None)
    return row


class CreateMembership(Body):
    user_id: str
    organisation_id: str | None = None
    role_codes: list[str] = Field(min_length=1)


@router.post("/memberships", status_code=201)
def create_membership(body: CreateMembership,
                      principal: Principal = Depends(require_roles(RoleCode.PLATFORM_ADMIN)),
                      session: Session = Depends(get_session, scope="function")):
    unknown = [r for r in body.role_codes if r not in _KNOWN_ROLES]
    if unknown:
        raise ApiError("VALIDATION_ERROR", "unknown role code",
                       details=[{"field": "role_codes", "reason": f"unknown: {unknown}"}])
    _row_or_404(session, "app_user", body.user_id, "user")
    if body.organisation_id:
        _row_or_404(session, "organisation", body.organisation_id, "organisation")
    platform = {RoleCode.OPS_REVIEWER, RoleCode.INDEX_PHARMACIST, RoleCode.COMPLIANCE_OFFICER,
                RoleCode.PLATFORM_ADMIN, RoleCode.SUPPORT_AGENT, RoleCode.PLATFORM_FINANCE}
    has_platform = any(r in platform for r in body.role_codes)
    if has_platform and body.organisation_id:
        raise ApiError("VALIDATION_ERROR", "a platform role cannot be attached to an organisation")
    if not has_platform and not body.organisation_id:
        raise ApiError("VALIDATION_ERROR", "an organisation role requires organisation_id")
    mid = new_id("mem")
    session.execute(
        text("INSERT INTO membership (id, user_id, organisation_id, role_codes, status) "
             "VALUES (:id, :u, :o, :r, 'ACTIVE')"),
        {"id": mid, "u": body.user_id, "o": body.organisation_id, "r": body.role_codes},
    )
    _audit(session, principal, action="ROLE_MEMBERSHIP_CHANGE",
           subject_type="membership", subject_id=mid,
           metadata=json.dumps({"role_codes": body.role_codes}))
    return _row_or_404(session, "membership", mid, "membership")


@router.get("/memberships")
def list_memberships(user_id: str | None = None, organisation_id: str | None = None,
                     principal: Principal = Depends(require_roles(RoleCode.PLATFORM_ADMIN, RoleCode.SUPPORT_AGENT)),
                     session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM membership WHERE (CAST(:u AS TEXT) IS NULL OR user_id=:u) "
             "AND (CAST(:o AS TEXT) IS NULL OR organisation_id=:o) ORDER BY created_at DESC LIMIT 200"),
        {"u": user_id, "o": organisation_id},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.post("/memberships/{membership_id}/revoke")
def revoke_membership(membership_id: str,
                      principal: Principal = Depends(require_roles(RoleCode.PLATFORM_ADMIN)),
                      session: Session = Depends(get_session, scope="function")):
    row = _row_or_404(session, "membership", membership_id, "membership")
    if row["status"] != "ACTIVE":
        raise ApiError("ILLEGAL_TRANSITION", "membership is not ACTIVE")
    session.execute(
        text("UPDATE membership SET status='REVOKED', revoked_at=:t, updated_at=:t WHERE id=:id AND status='ACTIVE'"),
        {"t": now(), "id": membership_id},
    )
    _audit(session, principal, action="ROLE_MEMBERSHIP_CHANGE",
           subject_type="membership", subject_id=membership_id,
           metadata=json.dumps({"revoked": True}))
    return _row_or_404(session, "membership", membership_id, "membership")


# ----------------------------------------------------------------- regions

@router.get("/regions")
def list_regions(principal: Principal = Depends(require_roles(*[r for r in RoleCode])),
                 session: Session = Depends(get_session, scope="function")):
    rows = session.execute(text("SELECT * FROM region ORDER BY code")).mappings().all()
    return {"items": [dict(r) for r in rows]}
