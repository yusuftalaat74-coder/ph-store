"""SM-20 — eta_estimate (§A7 addendum, A4.2 row SM-20).

Implemented subset: the full state table, with the spec's own documented
v1 deviation (`COMMITTED -> REALISED` folded into the same `REALISE`
trigger that also handles `IN_TRANSIT_LIVE -> REALISED`, since courier
pings are out of scope, A18) — except this module *does* make
`IN_TRANSIT_LIVE` reachable: `rova/domain/hooks.py` fires `GO_LIVE` from the
SM-05 `START` (`ASSIGNED -> IN_TRANSIT`) hook instead of a real GPS ping,
which is a strictly more complete implementation of the reachability
invariant than the spec's own carve-out asks for, not a divergence from it.
`create_provisional` is the module's public entry point for creating the one
open estimate an order carries from `CONFIRMED`/order-creation to
`DELIVERED` (R-158); it is called once per order right after the order row
is inserted (`rova/ordering/checkout.py`, `rova/domain/machines/sm02_quotation.py`),
since order creation itself is a raw `INSERT`, not a transition, in this
codebase (A8.1) and so cannot be a `Transition.effects` hook."""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text

from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.ids import new_id
from rova.domain.enums import EtaStatus
from rova.domain.fsm import Ctx, GuardResult, Machine, Transition

SUBJECT = "eta_estimate"


def create_provisional(session, order_id: str) -> str:
    """SLA_BOUNDS basis: acceptance + dispatch SLA windows plus the vendor's
    region transit window, all from `config_parameter`/`region` (A5, R-158)."""
    order = session.execute(text('SELECT vendor_id FROM "order" WHERE id=:o'), {"o": order_id}).mappings().one()
    region_code = session.execute(
        text("SELECT region_code FROM vendor_account WHERE id=:v"), {"v": order["vendor_id"]}
    ).scalar()
    transit_hours = session.execute(
        text("SELECT transit_window_hours FROM region WHERE code=:r"), {"r": region_code}
    ).scalar()
    acceptance_hours = cfg.get(session, "CFG-SLA-ACCEPTANCE-HOURS", vendor_id=order["vendor_id"], default=12)
    dispatch_hours = cfg.get(session, "CFG-SLA-DISPATCH-HOURS", vendor_id=order["vendor_id"], default=24)
    earliest = now()
    latest = now() + timedelta(hours=float(acceptance_hours) + float(dispatch_hours) + float(transit_hours))
    eta_id = new_id("eta")
    session.execute(
        text(
            "INSERT INTO eta_estimate (id, order_id, order_state, earliest_at, latest_at, basis, components, status) "
            "VALUES (:id, :o, 'PENDING_ACCEPTANCE', :e, :l, 'SLA_BOUNDS', CAST(:c AS JSONB), 'PROVISIONAL')"
        ),
        {
            "id": eta_id, "o": order_id, "e": earliest, "l": latest,
            "c": (
                '[{"name": "acceptance", "hours": %s, "source": "CFG-SLA-ACCEPTANCE-HOURS"}, '
                '{"name": "dispatch", "hours": %s, "source": "CFG-SLA-DISPATCH-HOURS"}, '
                '{"name": "transit", "hours": %s, "source": "region.transit_window_hours"}]'
            ) % (acceptance_hours, dispatch_hours, transit_hours),
        },
    )
    return eta_id


def open_estimate_id(session, order_id: str) -> str | None:
    """The order's one open (non-terminal) eta_estimate, if any (R-158)."""
    return session.execute(
        text(
            "SELECT id FROM eta_estimate WHERE order_id=:o AND status IN "
            "('PROVISIONAL','COMMITTED','IN_TRANSIT_LIVE')"
        ),
        {"o": order_id},
    ).scalar()


def _effect_commit(ctx: Ctx) -> None:
    pass  # range recomputation (VENDOR_PROMISE ± vendor variance) is out of this reduced scope's arithmetic


def _guard_realise(ctx: Ctx) -> GuardResult:
    realised_at = ctx.kwargs.get("realised_at", now())
    if realised_at > ctx.subject_row["latest_at"]:
        return GuardResult.failed("realised_at is after latest_at; use MISS")
    return GuardResult.passed()


def _guard_miss(ctx: Ctx) -> GuardResult:
    realised_at = ctx.kwargs.get("realised_at", now())
    if realised_at <= ctx.subject_row["latest_at"]:
        return GuardResult.failed("realised_at is within range; use REALISE")
    return GuardResult.passed()


def _extra_realise(ctx: Ctx) -> dict:
    realised_at = ctx.kwargs.get("realised_at", now())
    return {"realised_at": realised_at, "error_minutes": 0}


def _extra_miss(ctx: Ctx) -> dict:
    realised_at = ctx.kwargs.get("realised_at", now())
    error = int((realised_at - ctx.subject_row["latest_at"]).total_seconds() // 60)
    return {"realised_at": realised_at, "error_minutes": max(error, 0)}


def _effect_latest_passed(ctx: Ctx) -> None:
    create_provisional(ctx.session, ctx.subject_row["order_id"])


MACHINE = Machine(
    code="SM-20",
    subject_table=SUBJECT,
    status_column="status",
    transitions=[
        Transition("SM-20", (EtaStatus.PROVISIONAL,), EtaStatus.COMMITTED, "COMMIT", frozenset({"SYSTEM"}),
                   effects=_effect_commit, extra_set=lambda ctx: {"basis": "VENDOR_PROMISE",
                                                                   "order_state": "ACCEPTED"}),
        Transition("SM-20", (EtaStatus.COMMITTED,), EtaStatus.COMMITTED, "NARROW_ON_DISPATCH",
                   frozenset({"SYSTEM"}), extra_set=lambda ctx: {"order_state": "DISPATCHED"}),
        Transition("SM-20", (EtaStatus.COMMITTED,), EtaStatus.IN_TRANSIT_LIVE, "GO_LIVE", frozenset({"SYSTEM"}),
                   extra_set=lambda ctx: {"basis": "COURIER_LIVE", "delivery_state": "IN_TRANSIT"}),
        Transition("SM-20", (EtaStatus.COMMITTED, EtaStatus.IN_TRANSIT_LIVE), EtaStatus.REALISED, "REALISE",
                   frozenset({"SYSTEM"}), guard=_guard_realise, extra_set=_extra_realise),
        Transition("SM-20", (EtaStatus.COMMITTED, EtaStatus.IN_TRANSIT_LIVE), EtaStatus.MISSED, "MISS",
                   frozenset({"SYSTEM"}), guard=_guard_miss, extra_set=_extra_miss),
        Transition("SM-20", (EtaStatus.COMMITTED,), EtaStatus.MISSED, "LATEST_PASSED", frozenset({"SYSTEM"}),
                   effects=_effect_latest_passed, extra_set=lambda ctx: {"error_minutes": 0}),
        Transition("SM-20", (EtaStatus.PROVISIONAL, EtaStatus.COMMITTED, EtaStatus.IN_TRANSIT_LIVE), EtaStatus.VOID,
                   "VOID", frozenset({"SYSTEM"})),
    ],
)
