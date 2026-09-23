"""Operations console.

Everything here is read-first. The console's job is to let the team see the
platform without opening a psql prompt; the few writes it does have are
configuration and mode switches, both of which already had machinery and
neither of which is re-implemented here.

Two things this module deliberately does not do:

* It does not compute a second version of any number. Vendor scores,
  exposure, gate results and ranking audits are read from where the domain
  already wrote them.
* It does not let the console edit history. `state_transition`, `audit_event`
  and `ranking_isolation_audit` are append-only in the database; the console
  reads them and nothing more.
"""
import json
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.money import money_str
from rova.domain.enums import RoleCode
from rova.domain.machines.registry import MACHINES

router = APIRouter(prefix="/v1/ops", tags=["ops"])

_OPS = (RoleCode.OPS_REVIEWER, RoleCode.PLATFORM_ADMIN, RoleCode.COMPLIANCE_OFFICER)
_OPS_WIDE = _OPS + (RoleCode.SUPPORT_AGENT, RoleCode.PLATFORM_FINANCE, RoleCode.INDEX_PHARMACIST)
_ADMIN = (RoleCode.PLATFORM_ADMIN,)

SwitchKey = Literal["VENDOR_MODE", "FEE_MODEL", "PRIMARY_INTERFACE"]
ScopeType = Literal["GLOBAL", "REGION", "PHARMACY", "VENDOR"]
ConfigScope = Literal["GLOBAL", "REGION", "VENDOR"]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _audit(session: Session, principal: Principal, *, action: str, subject_type: str, subject_id: str,
           rule_ref: str | None = None, metadata: dict | None = None) -> None:
    session.execute(
        text("INSERT INTO audit_event (id, actor_user_id, actor_role, action_code, subject_type, subject_id, "
             "rule_ref, metadata, occurred_at) VALUES (:id, :u, :ar, :a, :st, :si, :rr, CAST(:m AS JSONB), :t)"),
        {"id": new_id("aud"), "u": principal.user_id,
         "ar": next(iter(sorted(principal.roles)), "UNKNOWN"), "a": action, "st": subject_type,
         "si": subject_id, "rr": rule_ref, "m": json.dumps(metadata or {}), "t": now()},
    )


# ------------------------------------------------------------------ dashboard

@router.get("/dashboard")
def dashboard(principal: Principal = Depends(require_roles(*_OPS_WIDE)),
              session: Session = Depends(get_session, scope="function")):
    """The one screen the operations team opens first: what is stuck, what is
    waiting on a person, and what is overdue."""
    counts = session.execute(text(
        "SELECT "
        " (SELECT count(*) FROM request WHERE status='NORMALIZING') AS requests_normalizing,"
        " (SELECT count(*) FROM request WHERE status='AWAITING_CONFIRMATION') AS requests_awaiting_confirmation,"
        " (SELECT count(*) FROM \"order\" WHERE status='PENDING_ACCEPTANCE') AS orders_pending_acceptance,"
        " (SELECT count(*) FROM \"order\" WHERE status='ACCEPTED') AS orders_accepted,"
        " (SELECT count(*) FROM \"order\" WHERE status='DISPATCHED') AS orders_dispatched,"
        " (SELECT count(*) FROM \"order\" WHERE status='DISPUTED') AS orders_disputed,"
        " (SELECT count(*) FROM dispute WHERE status IN ('OPEN','UNDER_REVIEW','ESCALATED')) AS disputes_open,"
        " (SELECT count(*) FROM return WHERE status NOT IN ('CLOSED','REJECTED')) AS returns_open,"
        " (SELECT count(*) FROM verification_case WHERE decision='PENDING') AS verifications_pending,"
        " (SELECT count(*) FROM support_thread WHERE active_handler='ESCALATING') AS threads_awaiting_human,"
        " (SELECT count(*) FROM sla_timer WHERE status='RUNNING' AND expires_at <= now()) AS timers_overdue,"
        " (SELECT count(*) FROM sla_timer WHERE status='RUNNING') AS timers_running,"
        " (SELECT count(*) FROM invoice WHERE status='PRICE_DEVIATION_FLAGGED') AS invoices_flagged,"
        " (SELECT count(*) FROM notification WHERE status='QUEUED') AS notifications_queued,"
        " (SELECT count(*) FROM demand_gap) AS demand_gaps"
    )).mappings().one()
    money = session.execute(text(
        "SELECT COALESCE(SUM(total_amount), 0) AS outstanding FROM invoice "
        "WHERE status IN ('AWAITING_PAYMENT','PARTIALLY_PAID')"
    )).mappings().one()
    return {"generated_at": now(), "counts": dict(counts),
            "invoices_outstanding_total": money_str(money["outstanding"])}


@router.get("/queues/verification")
def verification_queue(limit: int = Query(default=50, ge=1, le=200),
                       principal: Principal = Depends(require_roles(*_OPS)),
                       session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT vc.*, o.legal_name, o.type AS organisation_type FROM verification_case vc "
             "JOIN organisation o ON o.id = vc.organisation_id WHERE vc.decision='PENDING' "
             "ORDER BY vc.opened_at ASC LIMIT :lim"), {"lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/queues/licences-expiring")
def licences_expiring(days: int = Query(default=60, ge=1, le=365),
                      principal: Principal = Depends(require_roles(*_OPS)),
                      session: Session = Depends(get_session, scope="function")):
    """Licences that lapse inside `days`. A lapsed alvará suspends the vendor
    and drops every undispatched line it holds (SM-06 `LICENCE_EXPIRED`), so
    this queue is not administrative housekeeping — it is the list of vendors
    about to stop trading."""
    rows = session.execute(
        text("SELECT * FROM licence WHERE status IN ('VALID','EXPIRING_SOON') "
             "AND expiry_date <= (CURRENT_DATE + make_interval(days => :d)) "
             "ORDER BY expiry_date ASC LIMIT 200"), {"d": days},
    ).mappings().all()
    return {"window_days": days, "items": [dict(r) for r in rows]}


@router.get("/queues/overdue-timers")
def overdue_timers(limit: int = Query(default=100, ge=1, le=500),
                   principal: Principal = Depends(require_roles(*_OPS)),
                   session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM sla_timer WHERE status='RUNNING' AND expires_at <= :n "
             "ORDER BY expires_at ASC LIMIT :lim"), {"n": now(), "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/sla-timers")
def list_timers(policy_type: str | None = None, status: str | None = None,
                subject_id: str | None = None, limit: int = Query(default=100, ge=1, le=500),
                principal: Principal = Depends(require_roles(*_OPS_WIDE)),
                session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM sla_timer WHERE (CAST(:pt AS TEXT) IS NULL OR policy_type=:pt) "
             "AND (CAST(:st AS TEXT) IS NULL OR status=:st) "
             "AND (CAST(:si AS TEXT) IS NULL OR subject_id=:si) "
             "ORDER BY expires_at ASC LIMIT :lim"),
        {"pt": policy_type, "st": status, "si": subject_id, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


# ------------------------------------------------------------------ orders view

@router.get("/orders")
def list_orders(status: str | None = None, vendor_id: str | None = None,
                pharmacy_id: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                principal: Principal = Depends(require_roles(*_OPS_WIDE)),
                session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text('SELECT o.*, v.trade_name AS vendor_name, p.trade_name AS pharmacy_name '
             'FROM "order" o JOIN vendor_account v ON v.id = o.vendor_id '
             "JOIN pharmacy_account p ON p.id = o.pharmacy_id "
             "WHERE (CAST(:st AS TEXT) IS NULL OR o.status=:st) "
             "AND (CAST(:v AS TEXT) IS NULL OR o.vendor_id=:v) "
             "AND (CAST(:p AS TEXT) IS NULL OR o.pharmacy_id=:p) "
             "ORDER BY o.created_at DESC LIMIT :lim"),
        {"st": status, "v": vendor_id, "p": pharmacy_id, "lim": limit},
    ).mappings().all()
    return {"items": [{**dict(r), "goods_total": money_str(r["goods_total"])} for r in rows],
            "next_cursor": None}


@router.get("/orders/{order_id}")
def order_detail(order_id: str,
                 principal: Principal = Depends(require_roles(*_OPS_WIDE)),
                 session: Session = Depends(get_session, scope="function")):
    """Everything about one order in one read — including the fee events kept
    structurally separate from the goods invoice, shown side by side here
    precisely so an operator can see that they are two different things."""
    order = session.execute(text('SELECT * FROM "order" WHERE id=:o'), {"o": order_id}).mappings().first()
    if order is None:
        raise ApiError("NOT_FOUND", "order not found")
    lines = session.execute(text("SELECT * FROM order_line WHERE order_id=:o"), {"o": order_id}).mappings().all()
    transitions = session.execute(
        text("SELECT * FROM state_transition WHERE subject_id=:o OR subject_id = ANY(:lines) "
             "ORDER BY occurred_at ASC"),
        {"o": order_id, "lines": [l["id"] for l in lines] or [""]},
    ).mappings().all()
    invoices = session.execute(text("SELECT * FROM invoice WHERE order_id=:o"), {"o": order_id}).mappings().all()
    fees = session.execute(
        text("SELECT fe.*, fs.type, fs.payer FROM fee_event fe JOIN fee_schedule fs ON fs.id = fe.fee_schedule_id "
             "WHERE fe.order_id=:o"), {"o": order_id},
    ).mappings().all()
    jobs = session.execute(text("SELECT * FROM delivery_job WHERE order_id=:o"),
                           {"o": order_id}).mappings().all()
    return {"order": {**dict(order), "goods_total": money_str(order["goods_total"])},
            "lines": [dict(l) for l in lines],
            "invoices": [{**dict(i), "total_amount": money_str(i["total_amount"])} for i in invoices],
            "platform_fees": [{**dict(f), "amount": money_str(f["amount"])} for f in fees],
            "delivery_jobs": [dict(j) for j in jobs],
            "transitions": [dict(t) for t in transitions]}


# ------------------------------------------------------------------ vendor health

@router.get("/vendor-scores")
def vendor_scores(vendor_id: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                  principal: Principal = Depends(require_roles(*_OPS_WIDE)),
                  session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM vendor_score WHERE (CAST(:v AS TEXT) IS NULL OR vendor_id=:v) "
             "ORDER BY computed_at DESC LIMIT :lim"), {"v": vendor_id, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/vendors/{vendor_id}/health")
def vendor_health(vendor_id: str,
                  principal: Principal = Depends(require_roles(*_OPS_WIDE)),
                  session: Session = Depends(get_session, scope="function")):
    vendor = session.execute(text("SELECT * FROM vendor_account WHERE id=:v"),
                             {"v": vendor_id}).mappings().first()
    if vendor is None:
        raise ApiError("NOT_FOUND", "vendor not found")
    latest = session.execute(
        text("SELECT * FROM vendor_score WHERE vendor_id=:v ORDER BY computed_at DESC LIMIT 1"),
        {"v": vendor_id},
    ).mappings().first()
    live = session.execute(
        text('SELECT status, count(*) AS n FROM "order" WHERE vendor_id=:v GROUP BY status'),
        {"v": vendor_id},
    ).mappings().all()
    offers = session.execute(
        text("SELECT freshness_state, count(*) AS n FROM vendor_offer WHERE vendor_id=:v "
             "GROUP BY freshness_state"), {"v": vendor_id},
    ).mappings().all()
    licences = session.execute(
        text("SELECT id, type, status, expiry_date FROM licence WHERE holder_type='VENDOR' AND holder_id=:v "
             "ORDER BY expiry_date ASC"), {"v": vendor_id},
    ).mappings().all()
    return {"vendor": dict(vendor), "latest_score": dict(latest) if latest else None,
            "orders_by_status": {r["status"]: r["n"] for r in live},
            "offers_by_freshness": {r["freshness_state"]: r["n"] for r in offers},
            "licences": [dict(l) for l in licences]}


# ------------------------------------------------------------------ audit & history

@router.get("/audit-events")
def audit_events(action_code: str | None = None, subject_id: str | None = None,
                 actor_user_id: str | None = None, limit: int = Query(default=100, ge=1, le=500),
                 principal: Principal = Depends(require_roles(*_OPS)),
                 session: Session = Depends(get_session, scope="function")):
    """Append-only at the database level. There is no write route here and
    there never will be one."""
    rows = session.execute(
        text("SELECT * FROM audit_event WHERE (CAST(:ac AS TEXT) IS NULL OR action_code=:ac) "
             "AND (CAST(:si AS TEXT) IS NULL OR subject_id=:si) "
             "AND (CAST(:au AS TEXT) IS NULL OR actor_user_id=:au) "
             "ORDER BY occurred_at DESC LIMIT :lim"),
        {"ac": action_code, "si": subject_id, "au": actor_user_id, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/state-transitions")
def state_transitions(machine: str | None = None, subject_id: str | None = None,
                      subject_type: str | None = None, limit: int = Query(default=100, ge=1, le=500),
                      principal: Principal = Depends(require_roles(*_OPS_WIDE)),
                      session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM state_transition WHERE (CAST(:m AS TEXT) IS NULL OR machine=:m) "
             "AND (CAST(:si AS TEXT) IS NULL OR subject_id=:si) "
             "AND (CAST(:st AS TEXT) IS NULL OR subject_type=:st) "
             "ORDER BY occurred_at DESC LIMIT :lim"),
        {"m": machine, "si": subject_id, "st": subject_type, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/machines")
def list_machines(principal: Principal = Depends(require_roles(*_OPS_WIDE))):
    """The state machines as they actually are in code, with every legal
    transition. The console renders this rather than a hand-drawn diagram
    that drifts."""
    out = []
    for code, machine in MACHINES.items():
        out.append({
            "code": code, "subject_table": machine.subject_table,
            "status_column": machine.status_column,
            "transitions": [
                {"trigger": t.trigger, "from": list(t.from_states), "to": t.to_state,
                 "actors": sorted(t.actors), "rules": list(t.rule_refs),
                 "has_guard": t.guard is not None}
                for t in machine.transitions
            ],
        })
    return {"items": out}


@router.get("/traceability/{order_id}")
def traceability(order_id: str,
                 principal: Principal = Depends(require_roles(*_OPS)),
                 session: Session = Depends(get_session, scope="function")):
    """Batch, lot, expiry and seal per line — what a recall or an inspection
    actually asks for. Append-only; this route reads it."""
    if not session.execute(text('SELECT 1 FROM "order" WHERE id=:o'), {"o": order_id}).first():
        raise ApiError("NOT_FOUND", "order not found")
    events = session.execute(
        text("SELECT * FROM traceability_event WHERE order_id=:o ORDER BY occurred_at ASC"),
        {"o": order_id},
    ).mappings().all()
    lines = session.execute(
        text("SELECT id, index_product_id, batch_number, lot_number, expiry_date, seal_ids "
             "FROM order_line WHERE order_id=:o"), {"o": order_id},
    ).mappings().all()
    return {"order_id": order_id, "lines": [dict(l) for l in lines],
            "events": [dict(e) for e in events]}


# ------------------------------------------------------------------ ranking isolation

@router.get("/ranking-audits")
def ranking_audits(result: Literal["PASS", "FAIL"] | None = None,
                   limit: int = Query(default=50, ge=1, le=200),
                   principal: Principal = Depends(require_roles(*_OPS)),
                   session: Session = Depends(get_session, scope="function")):
    """Evidence that ranking stayed blind to fees. This is the record the
    multi-vendor gate reads, so it is also the record that decides whether
    the platform is allowed to turn multi-vendor on at all."""
    rows = session.execute(
        text("SELECT * FROM ranking_isolation_audit WHERE (CAST(:r AS TEXT) IS NULL OR result=:r) "
             "ORDER BY run_at DESC LIMIT :lim"), {"r": result, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/ranking-audits/latest")
def latest_ranking_audit(principal: Principal = Depends(require_roles(*_OPS)),
                         session: Session = Depends(get_session, scope="function")):
    row = session.execute(
        text("SELECT * FROM ranking_isolation_audit ORDER BY run_at DESC LIMIT 1")
    ).mappings().first()
    if row is None:
        return {"audit": None,
                "note": "no ranking isolation audit has ever run; the multi-vendor gate cannot pass"}
    return {"audit": dict(row)}


# ------------------------------------------------------------------ mode switches

class ProposeSwitch(Body):
    switch_key: SwitchKey
    proposed_value: str = Field(min_length=1, max_length=100)
    scope_type: ScopeType = "GLOBAL"
    scope_id: str | None = None


@router.post("/mode-switches", status_code=201)
def propose_switch(body: ProposeSwitch,
                   principal: Principal = Depends(require_roles(*_ADMIN)),
                   session: Session = Depends(get_session, scope="function")):
    """Proposing a switch does not flip anything. The sequence is PROPOSE →
    CHECK_GATES → GATES_PASSED → FLIP, and `current_value` is written only
    inside SM-22 (R-140) — never from here."""
    if (body.scope_type == "GLOBAL") != (body.scope_id is None):
        raise ApiError("VALIDATION_ERROR", "scope_id is required for a non-GLOBAL scope and forbidden for GLOBAL")
    existing = session.execute(
        text("SELECT id, status FROM mode_switch WHERE switch_key=:k AND scope_type=:st "
             "AND scope_id IS NOT DISTINCT FROM :si ORDER BY created_at DESC LIMIT 1"),
        {"k": body.switch_key, "st": body.scope_type, "si": body.scope_id},
    ).mappings().first()
    if existing is None:
        raise ApiError("NOT_FOUND",
                       "no mode_switch row exists for this key and scope; it is seeded, not created here")
    session.execute(
        text("UPDATE mode_switch SET proposed_value=:pv, proposed_by_user_id=:u, updated_at=:n WHERE id=:i"),
        {"pv": body.proposed_value, "u": principal.user_id, "n": now(), "i": existing["id"]},
    )
    result = MACHINES["SM-22"].apply(session, existing["id"], "PROPOSE", principal)
    _audit(session, principal, action="MODE_SWITCH_FLIPPED", rule_ref="R-140",
           subject_type="mode_switch", subject_id=existing["id"],
           metadata={"stage": "PROPOSE", "proposed_value": body.proposed_value})
    return dict(result)


@router.get("/mode-switches")
def list_switches(switch_key: SwitchKey | None = None,
                  principal: Principal = Depends(require_roles(*_OPS_WIDE)),
                  session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM mode_switch WHERE (CAST(:k AS TEXT) IS NULL OR switch_key=:k) "
             "ORDER BY switch_key, created_at DESC"), {"k": switch_key},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/mode-switches/{switch_id}")
def get_switch(switch_id: str,
               principal: Principal = Depends(require_roles(*_OPS_WIDE)),
               session: Session = Depends(get_session, scope="function")):
    row = session.execute(text("SELECT * FROM mode_switch WHERE id=:i"),
                          {"i": switch_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "mode switch not found")
    transitions = session.execute(
        text("SELECT * FROM state_transition WHERE subject_type='mode_switch' AND subject_id=:i "
             "ORDER BY occurred_at ASC"), {"i": switch_id},
    ).mappings().all()
    return {**dict(row), "transitions": [dict(t) for t in transitions]}


def _switch_transition(trigger: str, actor_is_system: bool = False):
    def _endpoint(switch_id: str,
                  principal: Principal = Depends(require_roles(*_ADMIN)),
                  session: Session = Depends(get_session, scope="function")):
        if not session.execute(text("SELECT 1 FROM mode_switch WHERE id=:i"), {"i": switch_id}).first():
            raise ApiError("NOT_FOUND", "mode switch not found")
        actor = "SYSTEM" if actor_is_system else principal
        result = MACHINES["SM-22"].apply(session, switch_id, trigger, actor)
        _audit(session, principal, action="MODE_SWITCH_FLIPPED", rule_ref="R-140",
               subject_type="mode_switch", subject_id=switch_id, metadata={"stage": trigger})
        return dict(result)
    return _endpoint


router.add_api_route("/mode-switches/{switch_id}/check-gates", _switch_transition("CHECK_GATES", True),
                     methods=["POST"], name="check_gates", tags=["ops"])
router.add_api_route("/mode-switches/{switch_id}/gates-passed", _switch_transition("GATES_PASSED", True),
                     methods=["POST"], name="gates_passed", tags=["ops"])
router.add_api_route("/mode-switches/{switch_id}/gates-failed", _switch_transition("GATES_FAILED", True),
                     methods=["POST"], name="gates_failed", tags=["ops"])
router.add_api_route("/mode-switches/{switch_id}/flip", _switch_transition("FLIP"),
                     methods=["POST"], name="flip_switch", tags=["ops"])
router.add_api_route("/mode-switches/{switch_id}/rollback", _switch_transition("ROLLBACK"),
                     methods=["POST"], name="rollback_switch", tags=["ops"])


# ------------------------------------------------------------------ configuration

@router.get("/config")
def list_config(key_prefix: str | None = None, scope_type: ConfigScope | None = None,
                limit: int = Query(default=200, ge=1, le=500),
                principal: Principal = Depends(require_roles(*_OPS_WIDE)),
                session: Session = Depends(get_session, scope="function")):
    """Business parameters live in rows, not environment variables. This is
    the list of every one of them."""
    rows = session.execute(
        text("SELECT * FROM config_parameter WHERE (CAST(:kp AS TEXT) IS NULL OR key LIKE :like) "
             "AND (CAST(:st AS TEXT) IS NULL OR scope_type=:st) ORDER BY key, scope_type LIMIT :lim"),
        {"kp": key_prefix, "like": f"{key_prefix}%" if key_prefix else "%", "st": scope_type, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/config/{key}")
def get_config(key: str, vendor_id: str | None = None, region_code: str | None = None,
               principal: Principal = Depends(require_roles(*_OPS_WIDE)),
               session: Session = Depends(get_session, scope="function")):
    """The resolved value for a scope, alongside every row that could have
    supplied it — so an operator can see *why* the value is what it is."""
    rows = session.execute(text("SELECT * FROM config_parameter WHERE key=:k ORDER BY scope_type"),
                           {"k": key}).mappings().all()
    if not rows:
        raise ApiError("NOT_FOUND", "no such config parameter")
    resolved = cfg.get(session, key, vendor_id=vendor_id, region_code=region_code, default=None)
    return {"key": key, "resolved_value": str(resolved) if resolved is not None else None,
            "resolved_for": {"vendor_id": vendor_id, "region_code": region_code},
            "owner_role": cfg.get_owner_role(session, key),
            "rows": [dict(r) for r in rows]}


class PutConfig(Body):
    value: str = Field(min_length=1, max_length=500)
    scope_type: ConfigScope = "GLOBAL"
    scope_id: str | None = None
    reason: str = Field(min_length=1, max_length=500)


@router.put("/config/{key}")
def put_config(key: str, body: PutConfig,
               principal: Principal = Depends(require_roles(*_ADMIN)),
               session: Session = Depends(get_session, scope="function")):
    """Changing a business parameter is an audited act with a stated reason.
    A new scope row can be added; the GLOBAL row's `value_type` is what the
    value must parse as, and a value that will not cast is refused here
    rather than blowing up the first time some unrelated request reads it."""
    if (body.scope_type == "GLOBAL") != (body.scope_id is None):
        raise ApiError("VALIDATION_ERROR", "scope_id is required for a non-GLOBAL scope and forbidden for GLOBAL")
    global_row = session.execute(
        text("SELECT * FROM config_parameter WHERE key=:k AND scope_type='GLOBAL'"), {"k": key},
    ).mappings().first()
    if global_row is None:
        raise ApiError("NOT_FOUND", "no such config parameter")

    try:
        cfg._cast(body.value, global_row["value_type"])
    except Exception:
        raise ApiError("VALIDATION_ERROR",
                       f"value does not parse as {global_row['value_type']}",
                       details=[{"field": "value", "reason": f"expected {global_row['value_type']}"}])

    existing = session.execute(
        text("SELECT id, value FROM config_parameter WHERE key=:k AND scope_type=:st "
             "AND scope_id IS NOT DISTINCT FROM :si"),
        {"k": key, "st": body.scope_type, "si": body.scope_id},
    ).mappings().first()
    if existing:
        session.execute(
            text("UPDATE config_parameter SET value=:v, updated_at=:n WHERE id=:i"),
            {"v": body.value, "n": now(), "i": existing["id"]},
        )
        row_id, before = existing["id"], existing["value"]
    else:
        row_id, before = new_id("cfg"), None
        session.execute(
            text("INSERT INTO config_parameter (id, key, scope_type, scope_id, value, value_type, owner_role, "
                 "source_tag) VALUES (:id, :k, :st, :si, :v, :vt, :orl, 'OPS_CONSOLE')"),
            {"id": row_id, "k": key, "st": body.scope_type, "si": body.scope_id, "v": body.value,
             "vt": global_row["value_type"], "orl": global_row["owner_role"]},
        )
    _audit(session, principal, action="CONFIG_PARAMETER_CHANGE", subject_type="config_parameter",
           subject_id=row_id, metadata={"key": key, "before": before, "after": body.value,
                                        "reason": body.reason})
    return dict(session.execute(text("SELECT * FROM config_parameter WHERE id=:i"),
                                {"i": row_id}).mappings().one())


# ------------------------------------------------------------------ PMS links

@router.get("/pms-links")
def list_pms_links(status: str | None = None, pharmacy_id: str | None = None,
                   principal: Principal = Depends(require_roles(*_OPS_WIDE)),
                   session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT id, pharmacy_id, pms_tenant_id, status, product_key_mode, linked_at, last_inbound_at, "
             "last_outbound_at, revoked_at FROM pms_link "
             "WHERE (CAST(:st AS TEXT) IS NULL OR status=:st) "
             "AND (CAST(:p AS TEXT) IS NULL OR pharmacy_id=:p) ORDER BY created_at DESC LIMIT 200"),
        {"st": status, "p": pharmacy_id},
    ).mappings().all()
    # api_key_hash is never selected — there is no read path for it anywhere.
    return {"items": [dict(r) for r in rows]}


@router.get("/data-subject-requests")
def list_dsr(status: str | None = None,
             principal: Principal = Depends(require_roles(RoleCode.COMPLIANCE_OFFICER, RoleCode.PLATFORM_ADMIN)),
             session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT * FROM data_subject_request WHERE (CAST(:st AS TEXT) IS NULL OR status=:st) "
             "ORDER BY received_at ASC LIMIT 200"), {"st": status},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


class CreateDsr(Body):
    subject_user_id: str
    type: Literal["ACCESS", "EXPORT", "ERASURE"]


@router.post("/data-subject-requests", status_code=201)
def create_dsr(body: CreateDsr,
               principal: Principal = Depends(require_roles(RoleCode.COMPLIANCE_OFFICER,
                                                            RoleCode.PLATFORM_ADMIN,
                                                            RoleCode.SUPPORT_AGENT)),
               session: Session = Depends(get_session, scope="function")):
    if not session.execute(text("SELECT 1 FROM app_user WHERE id=:u"), {"u": body.subject_user_id}).first():
        raise ApiError("NOT_FOUND", "user not found")
    did = new_id("dsr")
    session.execute(
        text("INSERT INTO data_subject_request (id, subject_user_id, type, status, received_at) "
             "VALUES (:id, :u, :ty, 'RECEIVED', :t)"),
        {"id": did, "u": body.subject_user_id, "ty": body.type, "t": now()},
    )
    return dict(session.execute(text("SELECT * FROM data_subject_request WHERE id=:i"),
                                {"i": did}).mappings().one())


class DecideDsr(Body):
    status: Literal["IN_PROGRESS", "FULFILLED", "REJECTED"]
    result_ref: str | None = None


@router.post("/data-subject-requests/{request_id}/decide")
def decide_dsr(request_id: str, body: DecideDsr,
               principal: Principal = Depends(require_roles(RoleCode.COMPLIANCE_OFFICER,
                                                            RoleCode.PLATFORM_ADMIN)),
               session: Session = Depends(get_session, scope="function")):
    row = session.execute(text("SELECT * FROM data_subject_request WHERE id=:i"),
                          {"i": request_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "data subject request not found")
    if row["status"] in ("FULFILLED", "REJECTED"):
        raise ApiError("ILLEGAL_TRANSITION", "this request is already closed")
    terminal = body.status in ("FULFILLED", "REJECTED")
    session.execute(
        text("UPDATE data_subject_request SET status=:st, handled_by_user_id=:u, result_ref=:rr, "
             "fulfilled_at=:ft, updated_at=:n WHERE id=:i"),
        {"st": body.status, "u": principal.user_id, "rr": body.result_ref,
         "ft": now() if terminal else None, "n": now(), "i": request_id},
    )
    _audit(session, principal, action="DATA_SUBJECT_REQUEST_HANDLED",
           subject_type="data_subject_request", subject_id=request_id,
           metadata={"status": body.status})
    return dict(session.execute(text("SELECT * FROM data_subject_request WHERE id=:i"),
                                {"i": request_id}).mappings().one())
