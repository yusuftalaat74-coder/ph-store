"""A15.4 rows 80-91 — vendor price-list import (A12, E-065-E-067, SM-21).
The highest-value piece of this session's scope: upload -> auto column
mapping (per-vendor memory, §2 of مواصفة_استيراد_قائمة_الأسعار.md) ->
row-by-row validation + Index matching (A12.2 step 4, never a silent
partial import) -> versioned publish (§5/§6) with a governed product's price
always coming from `price_reference`, never the vendor's own sheet (R-009,
structurally enforced by the schema; this pipeline fails loudly instead of
trying, per the executor brief)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.pagination import clamp_limit, decode_cursor, encode_cursor, page_envelope
from rova.domain.enums import RoleCode
from rova.domain.machines.registry import MACHINES
from rova.pricelist import fingerprint as fp
from rova.pricelist import mapping as mapping_svc
from rova.pricelist import parser, publisher, report, validator

router = APIRouter(prefix="/v1", tags=["pricelist"])

_VENDOR_WRITE = (RoleCode.VENDOR_ADMIN, RoleCode.VENDOR_ORDER_DESK)
_VENDOR_READ = (RoleCode.VENDOR_ADMIN, RoleCode.VENDOR_ORDER_DESK, RoleCode.VENDOR_FINANCE)
_OPS = (RoleCode.OPS_REVIEWER,)
_PLATFORM_READ = (RoleCode.OPS_REVIEWER, RoleCode.PLATFORM_ADMIN)
_TEMPLATE_ROLES = (RoleCode.VENDOR_ADMIN, RoleCode.VENDOR_ORDER_DESK, RoleCode.OPS_REVIEWER)

_TEMPLATE_PATH = str(Path(__file__).resolve().parents[1] / "seed" / "fixtures" / "template.xlsx")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _vendor_scope_or_404(principal: Principal, vendor_id: str) -> None:
    if RoleCode.OPS_REVIEWER in principal.roles or RoleCode.PLATFORM_ADMIN in principal.roles:
        return
    if principal.vendor_id == vendor_id:
        return
    raise ApiError("NOT_FOUND", "vendor not found")


def _version_or_404(session: Session, principal: Principal, version_id: str) -> dict:
    row = session.execute(text("SELECT * FROM price_list_version WHERE id=:v"), {"v": version_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "price list version not found")
    if RoleCode.OPS_REVIEWER in principal.roles or RoleCode.PLATFORM_ADMIN in principal.roles \
            or RoleCode.INDEX_PHARMACIST in principal.roles:
        return dict(row)
    if principal.vendor_id != row["vendor_id"]:
        raise ApiError("NOT_FOUND", "price list version not found")
    return dict(row)


@router.get("/price-lists/template")
def get_template(principal: Principal = Depends(require_roles(*_TEMPLATE_ROLES))):
    return FileResponse(_TEMPLATE_PATH, filename="lista_de_precos_template.xlsx",
                         media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.post("/vendors/{vendor_id}/price-lists")
async def upload_price_list(
    vendor_id: str,
    file: UploadFile = File(...),
    partial_update: bool = Form(False),
    effective_from: str | None = Form(None),
    principal: Principal = Depends(require_roles(*_VENDOR_WRITE, *_OPS)),
    session: Session = Depends(get_session, scope="function"),
):
    _vendor_scope_or_404(principal, vendor_id)
    vendor = session.execute(text("SELECT id FROM vendor_account WHERE id=:v"), {"v": vendor_id}).mappings().first()
    if vendor is None:
        raise ApiError("NOT_FOUND", "vendor not found")

    data = await file.read()
    parsed = parser.parse(file.filename or "upload", data, vendor_id=vendor_id)
    file_fingerprint = fp.fingerprint(parsed.headers)

    version_number = session.execute(
        text("SELECT COALESCE(MAX(version_number), 0) + 1 FROM price_list_version WHERE vendor_id=:v"),
        {"v": vendor_id},
    ).scalar()
    version_id = new_id("plv")
    requested_effective_from = _parse_dt(effective_from)
    session.execute(
        text(
            "INSERT INTO price_list_version (id, vendor_id, version_number, source_file_ref, source_file_name, "
            "fingerprint, uploaded_by_user_id, partial_update, row_count, status) VALUES "
            "(:id, :v, :num, :ref, :name, :fp, :u, :partial, :rc, 'UPLOADED')"
        ),
        {"id": version_id, "v": vendor_id, "num": version_number, "ref": parsed.stored_ref,
         "name": file.filename, "fp": file_fingerprint, "u": principal.user_id, "partial": partial_update,
         "rc": len(parsed.rows)},
    )
    for i, raw_row in enumerate(parsed.rows):
        session.execute(
            text(
                "INSERT INTO price_list_row (id, version_id, row_index, raw_values, outcome) "
                "VALUES (:id, :v, :i, CAST(:raw AS JSONB), 'PENDING_REVIEW')"
            ),
            {"id": new_id("plr"), "v": version_id, "i": i, "raw": json.dumps({k: _jsonable(v) for k, v in raw_row.items()})},
        )

    active_mapping = mapping_svc.find_active_mapping(session, vendor_id=vendor_id, fingerprint=file_fingerprint)
    if active_mapping:
        session.execute(text("UPDATE price_list_version SET mapping_id=:m WHERE id=:v"),
                         {"m": active_mapping["id"], "v": version_id})
        mapping_svc.touch_last_used(session, active_mapping["id"])
        MACHINES["SM-21"].apply(session, version_id, "MAPPING_FOUND", "SYSTEM")
        _run_validation_and_advance(session, version_id, requested_effective_from, principal, vendor_id)
        mapping_status = "MAPPING_FOUND"
    else:
        MACHINES["SM-21"].apply(session, version_id, "MAPPING_NEEDED", "SYSTEM")
        mapping_status = "MAPPING_NEEDED"

    version = session.execute(text("SELECT * FROM price_list_version WHERE id=:v"), {"v": version_id}).mappings().one()
    return {"id": version_id, "version_number": version_number, "status": version["status"],
            "mapping_status": mapping_status, "row_count": len(parsed.rows), "fingerprint": file_fingerprint}


def _jsonable(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _run_validation_and_advance(session: Session, version_id: str, requested_effective_from, principal: Principal, vendor_id: str) -> dict:
    counters = validator.validate_version(session, version_id)
    report_ref = report.build_report(session, version_id, vendor_id=vendor_id)
    session.execute(text("UPDATE price_list_version SET report_ref=:r WHERE id=:v"), {"r": report_ref, "v": version_id})
    trigger = "ALL_REJECTED" if counters["all_rejected"] else "VALIDATED"
    MACHINES["SM-21"].apply(session, version_id, trigger, "SYSTEM")
    return counters


@router.get("/vendors/{vendor_id}/price-lists")
def list_price_lists(vendor_id: str, principal: Principal = Depends(require_roles(*_VENDOR_READ, *_PLATFORM_READ)),
                      session: Session = Depends(get_session, scope="function")):
    _vendor_scope_or_404(principal, vendor_id)
    rows = session.execute(
        text("SELECT * FROM price_list_version WHERE vendor_id=:v ORDER BY version_number DESC"), {"v": vendor_id}
    ).mappings().all()
    return {"items": [_version_out(r) for r in rows], "next_cursor": None}


def _version_out(r: dict) -> dict:
    return {
        "id": r["id"], "vendor_id": r["vendor_id"], "version_number": r["version_number"],
        "source_file_name": r["source_file_name"], "status": r["status"], "partial_update": r["partial_update"],
        "row_count": r["row_count"], "accepted_rows": r["accepted_rows"], "rejected_rows": r["rejected_rows"],
        "warning_rows": r["warning_rows"], "pending_rows": r["pending_rows"],
        "effective_from": r["effective_from"].isoformat() if r["effective_from"] else None,
        "went_live_at": r["went_live_at"].isoformat() if r["went_live_at"] else None,
        "uploaded_at": r["uploaded_at"].isoformat(), "report_ref": r["report_ref"], "mapping_id": r["mapping_id"],
    }


@router.get("/price-lists/{version_id}")
def get_price_list(version_id: str, principal: Principal = Depends(require_roles(*_VENDOR_READ, *_PLATFORM_READ)),
                    session: Session = Depends(get_session, scope="function")):
    version = _version_or_404(session, principal, version_id)
    diff = {"to_create": 0, "to_update": 0, "to_withdraw": 0}
    if version["status"] in ("VALIDATED", "SCHEDULED", "LIVE"):
        rows = session.execute(
            text("SELECT index_product_id FROM price_list_row WHERE version_id=:v AND outcome IN "
                 "('ACCEPTED','WARNING') AND index_product_id IS NOT NULL"),
            {"v": version_id},
        ).scalars().all()
        covered = set(rows)
        if covered:
            existing = session.execute(
                text("SELECT index_product_id FROM vendor_offer WHERE vendor_id=:v AND index_product_id = ANY(:p) "
                     "AND freshness_state <> 'WITHDRAWN'"),
                {"v": version["vendor_id"], "p": list(covered)},
            ).scalars().all()
            existing_set = set(existing)
            diff["to_update"] = len(existing_set)
            diff["to_create"] = len(covered) - len(existing_set)
        if not version["partial_update"]:
            all_live = session.execute(
                text("SELECT index_product_id FROM vendor_offer WHERE vendor_id=:v AND freshness_state <> 'WITHDRAWN'"),
                {"v": version["vendor_id"]},
            ).scalars().all()
            diff["to_withdraw"] = len([p for p in all_live if p not in covered])
    return {**_version_out(version), "diff": diff}


@router.get("/price-lists/{version_id}/preview")
def preview_price_list(version_id: str, principal: Principal = Depends(require_roles(*_VENDOR_READ, *_OPS)),
                        session: Session = Depends(get_session, scope="function")):
    version = _version_or_404(session, principal, version_id)
    rows = session.execute(
        text("SELECT row_index, raw_values FROM price_list_row WHERE version_id=:v ORDER BY row_index LIMIT 20"),
        {"v": version_id},
    ).mappings().all()
    headers = list(rows[0]["raw_values"].keys()) if rows else []
    suggestion = mapping_svc.suggest_mapping(session, headers)
    return {
        "headers": headers,
        "rows": [{"row_index": r["row_index"], "values": r["raw_values"]} for r in rows],
        "suggested_mapping": suggestion.mapping,
        "suggestion_scores": suggestion.scores,
        "required_fields": {
            "any_of": list(mapping_svc.REQUIRED_ANY_OF),
            "all_of": list(mapping_svc.REQUIRED_ALL_OF),
            "any_stock_of": list(mapping_svc.REQUIRED_ANY_STOCK),
        },
    }


class MappingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping: dict[str, str | None]
    sheet_selector: str | None = None


@router.put("/price-lists/{version_id}/mapping")
def save_price_list_mapping(version_id: str, body: MappingBody,
                             principal: Principal = Depends(require_roles(RoleCode.VENDOR_ADMIN, *_OPS)),
                             session: Session = Depends(get_session, scope="function")):
    version = _version_or_404(session, principal, version_id)
    missing = mapping_svc.validate_mapping_complete(body.mapping)
    if missing:
        raise ApiError("VALIDATION_ERROR", "required canonical fields are not mapped",
                        details=[{"field": f, "reason": "required"} for f in missing])

    saved = mapping_svc.save_mapping(
        session, vendor_id=version["vendor_id"], fingerprint=version["fingerprint"], mapping=body.mapping,
        header_row_index=0, created_by_user_id=principal.user_id, sheet_selector=body.sheet_selector,
    )
    session.execute(text("UPDATE price_list_version SET mapping_id=:m WHERE id=:v"),
                     {"m": saved["id"], "v": version_id})
    MACHINES["SM-21"].apply(session, version_id, "MAPPING_SAVED", principal)
    counters = _run_validation_and_advance(session, version_id, None, principal, version["vendor_id"])
    updated = session.execute(text("SELECT * FROM price_list_version WHERE id=:v"), {"v": version_id}).mappings().one()
    return {**_version_out(updated), "counters": counters}


@router.get("/price-lists/{version_id}/rows")
def list_price_list_rows(version_id: str, outcome: str | None = None, limit: int | None = None, cursor: str | None = None,
                          principal: Principal = Depends(require_roles(*_VENDOR_READ, *_OPS, RoleCode.INDEX_PHARMACIST)),
                          session: Session = Depends(get_session, scope="function")):
    version = _version_or_404(session, principal, version_id)
    can_see_cost = (principal.vendor_id == version["vendor_id"]) or RoleCode.PLATFORM_FINANCE in principal.roles
    lim = clamp_limit(limit)
    params = {"v": version_id, "lim": lim + 1}
    where = "version_id=:v"
    if outcome:
        where += " AND outcome=:o"
        params["o"] = outcome
    if cursor:
        created_at, cid = decode_cursor(cursor)
        where += " AND (created_at, id) < (:ca, :cid)"
        params["ca"], params["cid"] = created_at, cid
    rows = session.execute(
        text(f"SELECT * FROM price_list_row WHERE {where} ORDER BY created_at DESC, id DESC LIMIT :lim"), params
    ).mappings().all()
    next_cursor = None
    if len(rows) > lim:
        rows = rows[:lim]
        next_cursor = encode_cursor(rows[-1]["created_at"], rows[-1]["id"])
    items = []
    for r in rows:
        d = dict(r)
        if not can_see_cost:
            d.pop("vendor_cost", None)
        items.append(d)
    return page_envelope(items, next_cursor)


@router.get("/price-lists/{version_id}/report")
def download_report(version_id: str, principal: Principal = Depends(require_roles(*_VENDOR_READ, *_OPS)),
                     session: Session = Depends(get_session, scope="function")):
    version = _version_or_404(session, principal, version_id)
    if not version["report_ref"]:
        raise ApiError("NOT_FOUND", "report not yet generated for this version")
    from rova.core.storage import full_path
    return FileResponse(str(full_path(version["report_ref"])), filename=f"relatorio_{version['version_number']}.xlsx",
                         media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effective_from: str | None = None


@router.post("/price-lists/{version_id}/publish")
def publish_price_list(version_id: str, body: PublishBody,
                        principal: Principal = Depends(require_roles(*_VENDOR_WRITE)),
                        session: Session = Depends(get_session, scope="function")):
    version = _version_or_404(session, principal, version_id)
    grace_hours = cfg.get(session, "CFG-PRICELIST-EFFECTIVE-GRACE-HOURS", default=24)
    requested = _parse_dt(body.effective_from) or now()
    if version["qty_only"]:
        effective_from = now()
    else:
        effective_from = max(now() + timedelta(hours=grace_hours), requested)

    MACHINES["SM-21"].apply(session, version_id, "PUBLISH", principal, effective_from=effective_from)
    result = {"status": "SCHEDULED", "effective_from": effective_from.isoformat()}
    if effective_from <= now():
        publish_result = publisher.go_live(session, version_id)
        MACHINES["SM-21"].apply(session, version_id, "GO_LIVE", "SYSTEM")
        result = {"status": "LIVE", "effective_from": effective_from.isoformat(), **publish_result}
    return result


@router.post("/price-lists/{version_id}/discard")
def discard_price_list(version_id: str, principal: Principal = Depends(require_roles(RoleCode.VENDOR_ADMIN)),
                        session: Session = Depends(get_session, scope="function")):
    version = _version_or_404(session, principal, version_id)
    trigger = {
        "UPLOADED": "ABANDON", "MAPPING_NEEDED": "ABANDON", "VALIDATING": "ABANDON",
        "VALIDATED": "DISCARD", "SCHEDULED": "CANCEL_SCHEDULED",
    }.get(version["status"])
    if trigger is None:
        raise ApiError("ILLEGAL_TRANSITION", f"cannot discard a version in status {version['status']}")
    updated = MACHINES["SM-21"].apply(session, version_id, trigger, principal)
    return _version_out(dict(updated))


class ResolveRowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index_product_id: str | None = None
    propose_alias: str | None = None


@router.post("/price-list-rows/{row_id}/resolve")
def resolve_price_list_row(row_id: str, body: ResolveRowBody,
                            principal: Principal = Depends(require_roles(*_OPS, RoleCode.INDEX_PHARMACIST)),
                            session: Session = Depends(get_session, scope="function")):
    row = session.execute(text("SELECT * FROM price_list_row WHERE id=:id"), {"id": row_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "price list row not found")
    result = publisher.resolve_pending_row(session, row_id, index_product_id=body.index_product_id)
    session.execute(text("UPDATE price_list_row SET resolved_by_user_id=:u WHERE id=:id"),
                     {"u": principal.user_id, "id": row_id})
    return result


@router.get("/vendors/{vendor_id}/column-mappings")
def list_column_mappings(vendor_id: str, principal: Principal = Depends(require_roles(RoleCode.VENDOR_ADMIN, *_OPS, RoleCode.PLATFORM_ADMIN)),
                          session: Session = Depends(get_session, scope="function")):
    _vendor_scope_or_404(principal, vendor_id)
    rows = session.execute(
        text("SELECT * FROM vendor_column_mapping WHERE vendor_id=:v ORDER BY created_at DESC"), {"v": vendor_id}
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}
