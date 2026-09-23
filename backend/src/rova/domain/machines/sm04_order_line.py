"""SM-04 — order_line / `fulfilment_status` (§6 SM-04, A4.2 row SM-04).

Implemented subset: `CONFIRM_FULL`, `CONFIRM_SHORT`, `POST_ACCEPTANCE_SHORTAGE`
(the post-acceptance `LINE_FULL -> LINE_SHORT` shortage path named in the
executor's task), `VENDOR_LICENCE_LAPSE`, `PHARMACY_LICENCE_LAPSE` (which
cascades straight to `LINE_DROPPED` with no reroute, §6), `COMPLIANCE_HALT`,
`AUTO_REROUTE`, `PHARMACY_REROUTE`, `ACCEPT_SUBSTITUTE`, `DROP`. Reroute
creates a new `request_line` (`origin_line_id` set, `match_status=
'UNRESOLVED'`) but does not itself allocate it — `rova/ordering/
checkout.py::allocate_remainder` (`POST /v1/request-lines/{id}/allocate`,
defect 4 fix) is the actual re-entry into ranking/allocation for that line,
excluding this line's own vendor (R-029)."""
from sqlalchemy import text

from rova.core.clock import now
from rova.core.ids import new_id
from rova.domain import timers
from rova.domain.enums import OrderLineFulfilmentStatus, RoleCode
from rova.domain.fsm import Ctx, GuardResult, Machine, Transition

_VENDOR_DESK = frozenset({RoleCode.VENDOR_ORDER_DESK, RoleCode.VENDOR_ADMIN})
_VENDOR_PICKER = frozenset({RoleCode.VENDOR_PICKER, RoleCode.VENDOR_ORDER_DESK, RoleCode.VENDOR_ADMIN})
_BUYER = frozenset({RoleCode.PHARMACY_BUYER, RoleCode.PHARMACY_ADMIN})

SUBJECT = "order_line"
POLICY = "REROUTE_ASK"


def _order(ctx: Ctx) -> dict:
    return dict(ctx.session.execute(
        text('SELECT * FROM "order" WHERE id=:o'), {"o": ctx.subject_row["order_id"]}
    ).mappings().one())


def _pharmacy_auto_reroute(ctx: Ctx, order: dict) -> bool:
    return bool(ctx.session.execute(
        text("SELECT auto_reroute FROM pharmacy_account WHERE id=:p"), {"p": order["pharmacy_id"]}
    ).scalar())


def _guard_confirm_full(ctx: Ctx) -> GuardResult:
    order = _order(ctx)
    if order["status"] != "PENDING_ACCEPTANCE":
        return GuardResult.failed("order is not PENDING_ACCEPTANCE")
    return GuardResult.passed()


def _guard_post_acceptance(ctx: Ctx) -> GuardResult:
    order = _order(ctx)
    if order["status"] != "ACCEPTED":
        return GuardResult.failed("order must be ACCEPTED and not yet dispatched")
    beyond_created = ctx.session.execute(
        text("SELECT 1 FROM delivery_job WHERE order_id=:o AND status <> 'CREATED'"), {"o": order["id"]}
    ).first()
    if beyond_created:
        return GuardResult.failed("delivery job has moved beyond CREATED")
    return GuardResult.passed()


def _start_reroute_timer_if_needed(ctx: Ctx, order: dict) -> None:
    if not _pharmacy_auto_reroute(ctx, order):
        timers.start(ctx.session, policy_type=POLICY, subject_type=SUBJECT, subject_id=ctx.subject_id,
                      cfg_key="CFG-REROUTE-ASK-TIMEOUT-HOURS", unit="HOURS")


def _effect_confirm_short(ctx: Ctx) -> None:
    order = _order(ctx)
    _start_reroute_timer_if_needed(ctx, order)


def _extra_set_confirm_short(ctx: Ctx) -> dict:
    extra = {"short_reason": "PARTIAL_ACCEPTANCE"}
    if "confirmed_qty" in ctx.kwargs:
        extra["confirmed_qty"] = ctx.kwargs["confirmed_qty"]
    return extra


def _effect_post_acceptance_shortage(ctx: Ctx) -> None:
    order = _order(ctx)
    reason = ctx.kwargs.get("short_reason", "QTY_CORRECTION")
    ctx.session.execute(
        text("INSERT INTO audit_event (id, actor_role, action_code, subject_type, subject_id, rule_ref, metadata) "
             "VALUES (:id, :role, 'ORDER_LINE_QTY_CORRECTION', :st, :sid, 'R-121', "
             "CAST(:meta AS JSONB))"),
        {"id": new_id("aud"), "role": ctx.actor_role, "st": SUBJECT, "sid": ctx.subject_id,
         "meta": '{"reason": "%s"}' % reason},
    )
    _start_reroute_timer_if_needed(ctx, order)
    # the extra_set UPDATE (which may have zeroed confirmed_qty, e.g. a
    # LICENCE_LAPSE/COMPLIANCE_HALT cascade, or a QTY_CORRECTION down to 0)
    # already committed by the time effects run (A4.1 apply() ordering), so
    # this reads the true post-update total rather than ctx.subject_row's
    # pre-transition snapshot.
    from rova.domain.machines.sm03_order import maybe_fire_nothing_to_dispatch
    maybe_fire_nothing_to_dispatch(ctx.session, order["id"])


def _effect_pharmacy_licence_lapse(ctx: Ctx) -> None:
    # no reroute timer, no substitute — cascades straight to LINE_DROPPED
    # (SYSTEM only) within the same transaction, per §6 SM-04.
    MACHINE.apply(ctx.session, ctx.subject_id, "DROP", "SYSTEM")
    from rova.domain.machines.sm03_order import maybe_fire_nothing_to_dispatch
    maybe_fire_nothing_to_dispatch(ctx.session, ctx.subject_row["order_id"])


def _guard_reroute(ctx: Ctx) -> GuardResult:
    order = _order(ctx)
    if order["status"] == "ACCEPTED":
        pharmacy_suspended = ctx.session.execute(
            text("SELECT status='SUSPENDED' AND suspension_cause='LICENCE_EXPIRED' FROM pharmacy_account WHERE id=:p"),
            {"p": order["pharmacy_id"]},
        ).scalar()
        if pharmacy_suspended:
            return GuardResult.failed("pharmacy is suspended for an expired licence; no reroute possible", rule="R-121")
    return GuardResult.passed()


def _effect_reroute(ctx: Ctx) -> None:
    """Defect 4 fix: this used to insert the remainder `request_line` with
    `match_status='RESOLVED'` and a docstring (now removed) claiming it
    "re-enters the ordinary checkout path on its own" — false, since
    `checkout()` requires `request.status='DRAFT'` and this request is long
    past that. `match_status='UNRESOLVED'` is the honest state: nothing has
    allocated this line yet. `rova/ordering/checkout.py::allocate_remainder`
    (called via `POST /v1/request-lines/{id}/allocate`) is the real re-entry
    point — it ranks the remainder (excluding this line's own vendor, R-029)
    and either creates a follow-on order or leaves the line `UNRESOLVED`,
    still visible on the parent request either way."""
    line = ctx.subject_row
    remainder = line["ordered_qty"] - line["confirmed_qty"]
    session = ctx.session
    request_line = session.execute(
        text("SELECT * FROM request_line WHERE id=:id"), {"id": line["request_line_id"]}
    ).mappings().one()
    substitution_id = ctx.kwargs.get("substitution_id")
    product_id = request_line["index_product_id"]
    if substitution_id:
        product_id = session.execute(
            text("SELECT substitute_product_id FROM substitution WHERE id=:s"), {"s": substitution_id}
        ).scalar()
    session.execute(
        text(
            "INSERT INTO request_line (id, request_id, index_product_id, qty_requested, line_kind, match_status, "
            "substitution_id, origin_line_id) VALUES (:id, :rid, :pid, :qty, :kind, 'UNRESOLVED', :sub, :origin)"
        ),
        {"id": new_id("rql"), "rid": request_line["request_id"],
         "pid": product_id, "qty": remainder, "kind": request_line["line_kind"],
         "sub": substitution_id, "origin": request_line["id"]},
    )
    timers.cancel(session, policy_type=POLICY, subject_type=SUBJECT, subject_id=ctx.subject_id)


def _effect_drop(ctx: Ctx) -> None:
    timers.cancel(ctx.session, policy_type=POLICY, subject_type=SUBJECT, subject_id=ctx.subject_id)
    # A8.5: "every drop writes demand_gap(LINE_DROPPED_AFTER_SHORT)"
    session = ctx.session
    line = ctx.subject_row
    order = _order(ctx)
    request_line = session.execute(
        text("SELECT index_product_id, request_id FROM request_line WHERE id=:id"), {"id": line["request_line_id"]}
    ).mappings().one()
    remainder = line["ordered_qty"] - line["confirmed_qty"]
    if remainder > 0:
        session.execute(
            text(
                "INSERT INTO demand_gap (id, index_product_id, pharmacy_id, region_code, qty_requested, cause, "
                "request_line_id) VALUES (:id, :pid, :pha, :region, :qty, 'LINE_DROPPED_AFTER_SHORT', :rlid)"
            ),
            {"id": new_id("dgp"), "pid": request_line["index_product_id"], "pha": order["pharmacy_id"],
             "region": session.execute(
                 text("SELECT region_code FROM pharmacy_account WHERE id=:p"), {"p": order["pharmacy_id"]}
             ).scalar(),
             "qty": remainder, "rlid": line["request_line_id"]},
        )
    from rova.domain.machines.sm03_order import maybe_fire_nothing_to_dispatch
    maybe_fire_nothing_to_dispatch(session, order["id"])


MACHINE = Machine(
    code="SM-04",
    subject_table=SUBJECT,
    status_column="fulfilment_status",
    transitions=[
        Transition("SM-04", (OrderLineFulfilmentStatus.LINE_PENDING,), OrderLineFulfilmentStatus.LINE_FULL,
                   "CONFIRM_FULL", _VENDOR_DESK | {"SYSTEM"}, guard=_guard_confirm_full,
                   extra_set=lambda ctx: {"confirmed_qty": ctx.subject_row["ordered_qty"]}),
        Transition("SM-04", (OrderLineFulfilmentStatus.LINE_PENDING,), OrderLineFulfilmentStatus.LINE_SHORT,
                   "CONFIRM_SHORT", _VENDOR_DESK | {"SYSTEM"}, effects=_effect_confirm_short,
                   extra_set=_extra_set_confirm_short),
        Transition("SM-04", (OrderLineFulfilmentStatus.LINE_FULL,), OrderLineFulfilmentStatus.LINE_SHORT,
                   "POST_ACCEPTANCE_SHORTAGE", _VENDOR_PICKER | {"SYSTEM"}, guard=_guard_post_acceptance,
                   effects=_effect_post_acceptance_shortage,
                   extra_set=lambda ctx: {
                       "short_reason": ctx.kwargs.get("short_reason", "QTY_CORRECTION"),
                       **({"confirmed_qty": ctx.kwargs["confirmed_qty"]} if "confirmed_qty" in ctx.kwargs else {}),
                   }),
        Transition("SM-04", (OrderLineFulfilmentStatus.LINE_FULL,), OrderLineFulfilmentStatus.LINE_SHORT,
                   "VENDOR_LICENCE_LAPSE", frozenset({"SYSTEM"}), guard=_guard_post_acceptance,
                   effects=_effect_post_acceptance_shortage,
                   extra_set=lambda ctx: {"short_reason": "LICENCE_LAPSE", "confirmed_qty": 0}),
        Transition("SM-04", (OrderLineFulfilmentStatus.LINE_FULL,), OrderLineFulfilmentStatus.LINE_SHORT,
                   "PHARMACY_LICENCE_LAPSE", frozenset({"SYSTEM"}), guard=_guard_post_acceptance,
                   effects=_effect_pharmacy_licence_lapse,
                   extra_set=lambda ctx: {"short_reason": "LICENCE_LAPSE", "confirmed_qty": 0}),
        Transition("SM-04", (OrderLineFulfilmentStatus.LINE_FULL,), OrderLineFulfilmentStatus.LINE_SHORT,
                   "COMPLIANCE_HALT", frozenset({"SYSTEM"}),
                   effects=_effect_post_acceptance_shortage,
                   extra_set=lambda ctx: {"short_reason": "COMPLIANCE_HALT", "confirmed_qty": 0}),
        Transition("SM-04", (OrderLineFulfilmentStatus.LINE_SHORT,), OrderLineFulfilmentStatus.LINE_REROUTED,
                   "AUTO_REROUTE", frozenset({"SYSTEM"}), guard=_guard_reroute, effects=_effect_reroute),
        Transition("SM-04", (OrderLineFulfilmentStatus.LINE_SHORT,), OrderLineFulfilmentStatus.LINE_REROUTED,
                   "PHARMACY_REROUTE", _BUYER, guard=_guard_reroute, effects=_effect_reroute),
        Transition("SM-04", (OrderLineFulfilmentStatus.LINE_SHORT,), OrderLineFulfilmentStatus.LINE_REROUTED,
                   "ACCEPT_SUBSTITUTE", _BUYER, guard=_guard_reroute, effects=_effect_reroute, rule_refs=("R-004",)),
        Transition("SM-04", (OrderLineFulfilmentStatus.LINE_SHORT,), OrderLineFulfilmentStatus.LINE_DROPPED,
                   "DROP", _BUYER | {"SYSTEM"}, effects=_effect_drop),
    ],
)
