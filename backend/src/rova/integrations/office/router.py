"""SPEC 5.9.2 router:

  POST /v1/office/events                 Office -> Store events (HMAC)
  GET  /v1/office/orders/{order_id}      pull the full order (HMAC)
  GET  /v1/office/reconcile/orders       nightly reconciliation (HMAC)
  GET  /v1/office/reconcile/facilities   nightly reconciliation (HMAC)
  POST /v1/office/outbox/replay          re-send from a seq (HMAC)
  GET  /v1/me/account                    the pharmacy's balance snapshot (JWT)
  GET  /v1/me/statement                  proxied to PH Office (JWT)

and the `require_not_office_managed` dependency added to the SPEC 1.3 routes.
While ROVA_OFFICE_ENABLED is false every /v1/office/* route answers 503 and
the dependency lets everything through: the live app behaves as before."""
import json
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.core.db import get_session, get_sessionmaker
from rova.core.errors import ApiError
from rova.core.money import money_str
from rova.domain.enums import RoleCode
from rova.integrations.office import config, emitter, inbound, signing

router = APIRouter(tags=["office-link"])
_PHARMACY = (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER, RoleCode.PHARMACY_RECEIVER)


def require_not_office_managed() -> None:
    """SPEC 5.9.6 — locks the vendor fulfilment/finance routes once PH Office owns them."""
    if config.enabled():
        raise ApiError("GUARD_FAILED", "managed by PH Office")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": []}})


def _disabled():
    return _error(503, "OFFICE_LINK_DISABLED", "the PH Office link is disabled (ROVA_OFFICE_ENABLED=false)")


def _signed(request: Request, raw: bytes) -> bool:
    return signing.verify(config.inbound_secret(), request.headers, raw)


@router.post("/v1/office/events")
async def office_events(request: Request):
    if not config.enabled():
        return _disabled()
    raw = await request.body()
    if not _signed(request, raw):
        return _error(401, "SIGNATURE_INVALID", "bad or stale signature")
    try:
        env = json.loads(raw)
    except ValueError:
        raise ApiError("VALIDATION_ERROR", "body is not JSON")
    s = get_sessionmaker()()
    try:
        iid, duplicate = inbound.record(s, env)
        s.commit()
    finally:
        s.close()
    if duplicate:
        return {"status": "duplicate", "inbound_event_id": iid}
    from rova.domain.hooks import wire
    wire()
    return {"status": "accepted", "inbound_event_id": iid, "processing": inbound.apply_in_new_transaction(iid)}


@router.get("/v1/office/orders/{order_id}")
def office_order(order_id: str, request: Request, session: Session = Depends(get_session, scope="function")):
    if not config.enabled():
        return _disabled()
    if not _signed(request, b""):
        return _error(401, "SIGNATURE_INVALID", "bad or stale signature")
    from rova.fulfilment.router import _order_with_lines
    if session.execute(text('SELECT 1 FROM "order" WHERE id=:o'), {"o": order_id}).first() is None:
        raise ApiError("NOT_FOUND", "order not found")
    out = _order_with_lines(session, order_id)
    out["receipt_lines"] = [dict(r) for r in session.execute(
        text("SELECT rl.* FROM receipt r JOIN receipt_line rl ON rl.receipt_id=r.id WHERE r.order_id=:o"),
        {"o": order_id}).mappings().all()]
    out["invoices"] = [dict(r) for r in session.execute(text("SELECT * FROM invoice WHERE order_id=:o"),
                                                        {"o": order_id}).mappings().all()]
    out["version"] = session.execute(text("SELECT COUNT(*) FROM state_transition WHERE subject_id=:o"), {"o": order_id}).scalar()
    return out


@router.get("/v1/office/reconcile/orders")
def office_reconcile_orders(request: Request, session: Session = Depends(get_session, scope="function")):
    if not config.enabled():
        return _disabled()
    if not _signed(request, b""):
        return _error(401, "SIGNATURE_INVALID", "bad or stale signature")
    qp = request.query_params
    clauses, params = ["TRUE"], {}
    if qp.get("from"):
        clauses.append("o.created_at >= :f")
        params["f"] = date.fromisoformat(qp["from"])
    if qp.get("to"):
        clauses.append("o.created_at < CAST(:t AS DATE) + 1")
        params["t"] = date.fromisoformat(qp["to"])
    rows = session.execute(text(
        "SELECT o.id AS order_id, o.number, o.status, o.goods_total, "
        "(SELECT COUNT(*) FROM state_transition st WHERE st.subject_id = o.id) AS version, "
        "(SELECT COALESCE(SUM(total_amount),0) FROM invoice i WHERE i.order_id = o.id) AS invoice_total, "
        "(SELECT COALESCE(SUM(pa.amount),0) FROM invoice i JOIN payment_allocation pa ON pa.invoice_id = i.id "
        " WHERE i.order_id = o.id) AS paid_total "
        f'FROM "order" o WHERE {" AND ".join(clauses)} ORDER BY o.created_at'), params).mappings().all()
    return {"items": [{**dict(r), "goods_total": money_str(r["goods_total"]), "invoice_total": money_str(r["invoice_total"]),
                       "paid_total": money_str(r["paid_total"])} for r in rows]}


@router.get("/v1/office/reconcile/facilities")
def office_reconcile_facilities(request: Request, session: Session = Depends(get_session, scope="function")):
    if not config.enabled():
        return _disabled()
    if not _signed(request, b""):
        return _error(401, "SIGNATURE_INVALID", "bad or stale signature")
    rows = session.execute(text("SELECT id AS facility_id, pharmacy_id, vendor_id, office_balance, office_synced_at, "
                                "limit_amount AS limit, office_credit_limit, office_hold FROM credit_facility")).mappings().all()
    return {"items": [{**dict(r), "office_balance": money_str(r["office_balance"]) if r["office_balance"] is not None else None,
                       "limit": money_str(r["limit"]),
                       "office_synced_at": r["office_synced_at"].isoformat() if r["office_synced_at"] else None}
                      for r in rows]}


class ReplayBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_seq: int


@router.post("/v1/office/outbox/replay")
async def office_outbox_replay(request: Request):
    if not config.enabled():
        return _disabled()
    raw = await request.body()
    if not _signed(request, raw):
        return _error(401, "SIGNATURE_INVALID", "bad or stale signature")
    body = ReplayBody.model_validate_json(raw)
    s = get_sessionmaker()()
    try:
        out = emitter.replay_from(s, body.from_seq)
        s.commit()
        return out
    finally:
        s.close()


@router.get("/v1/me/account")
def me_account(principal: Principal = Depends(require_roles(*_PHARMACY)),
               session: Session = Depends(get_session, scope="function")):
    """SPEC 5.8 — Conta: what the pharmacy owes, from the Office snapshot (as_of shown)."""
    if not principal.pharmacy_id:
        raise ApiError("FORBIDDEN", "pharmacy users only")
    snap = session.execute(text("SELECT office_account_snapshot FROM pharmacy_account WHERE id=:p"),
                           {"p": principal.pharmacy_id}).scalar() or {}
    vendors = snap.get("vendors", {})
    names = {r["id"]: r["trade_name"] for r in session.execute(
        text("SELECT id, trade_name FROM vendor_account WHERE id = ANY(:v)"), {"v": list(vendors)}).mappings().all()}
    accounts, totals = [], {"balance": Decimal("0"), "credit_limit": Decimal("0"), "overdue_amount": Decimal("0")}
    for vid, a in sorted(vendors.items()):
        bal, lim, over = Decimal(a["balance"]), Decimal(a["credit_limit"]), Decimal(a["overdue_amount"])
        totals["balance"] += bal
        totals["credit_limit"] += lim
        totals["overdue_amount"] += over
        accounts.append({"vendor_id": vid, "vendor_name": names.get(vid, vid), "balance": money_str(bal),
                         "credit_limit": money_str(lim), "available": money_str(max(lim - bal, Decimal("0"))),
                         "overdue_amount": money_str(over), "status": a.get("status"), "terms_days": a.get("terms_days"),
                         "oldest_due_date": a.get("oldest_due_date"), "open_documents": a.get("open_documents", []),
                         "ageing": a.get("ageing"), "as_of": a.get("as_of")})
    as_of = max((a["as_of"] for a in accounts if a["as_of"]), default=None)
    return {"pharmacy_id": principal.pharmacy_id, "currency": "MZN", "as_of": as_of, "source": "PH Office",
            "balance": money_str(totals["balance"]), "credit_limit": money_str(totals["credit_limit"]),
            "available": money_str(max(totals["credit_limit"] - totals["balance"], Decimal("0"))),
            "overdue_amount": money_str(totals["overdue_amount"]),
            "open_documents": [dict(d, vendor_id=a["vendor_id"]) for a in accounts for d in a["open_documents"]],
            "accounts": accounts}


@router.get("/v1/me/statement")
def me_statement(request: Request, vendor_id: str | None = None,
                 principal: Principal = Depends(require_roles(*_PHARMACY)),
                 session: Session = Depends(get_session, scope="function")):
    """SPEC 5.8 — the full statement comes from PH Office, signed; 503 when Office is unreachable."""
    if not principal.pharmacy_id:
        raise ApiError("FORBIDDEN", "pharmacy users only")
    if not config.enabled() or not config.url():
        return _error(503, "UPSTREAM_UNAVAILABLE", "PH Office is not linked")
    if vendor_id is None:
        snap = session.execute(text("SELECT office_account_snapshot FROM pharmacy_account WHERE id=:p"),
                               {"p": principal.pharmacy_id}).scalar() or {}
        vendors = list(snap.get("vendors", {}))
        if len(vendors) == 1:
            vendor_id = vendors[0]
    params = {k: v for k, v in (("vendor_id", vendor_id), ("from", request.query_params.get("from")),
                                ("to", request.query_params.get("to"))) if v}
    try:
        resp = emitter.http_client().get(
            f"{config.url()}/office/v1/integration/pharmacies/{principal.pharmacy_id}/statement",
            params=params, headers=signing.headers(config.outbound_secret(), b""))
    except Exception:
        return _error(503, "UPSTREAM_UNAVAILABLE", "PH Office is unreachable; showing the last snapshot is advised")
    if resp.status_code == 404:
        raise ApiError("NOT_FOUND", "no statement for this pharmacy")
    if resp.status_code != 200:
        return _error(503, "UPSTREAM_UNAVAILABLE", f"PH Office answered {resp.status_code}")
    return resp.json()
