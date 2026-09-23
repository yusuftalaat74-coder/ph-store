"""SM-03 — order (A4.2 row SM-03).

Implemented subset: `ACCEPT`, `AUTO_ACCEPT` (R-128), `REJECT`,
`PHARMACY_CANCEL` (before acceptance only), `DISPATCH`, `DELIVERED`,
`RECEIPT_ACCEPT`, `CLOSE` — plus, added in the SM-05/08/09/10/11/jobs
session (this module is not itself owned by that scope, but
`DISPUTED`/`RETURN_IN_PROGRESS` are unreachable without these, which is a
reachability bug against A4.1's own invariant): `ACCEPTANCE_TIMEOUT`,
`DISPATCH_TIMEOUT`, `DELIVERY_EXHAUSTED` (fired by SM-05), `RECEIPT_DISPUTE`,
`DISPUTE_RESOLVED_ACCEPT`, `DISPUTE_RESOLVED_RETURN`, `RMA_REQUESTED`,
`RETURN_CLOSED`. SlaTimer(ACCEPTANCE)/SlaTimer(DISPATCH) are started here
too (cancelled on ACCEPT/DISPATCH, started on ACCEPT->dispatch and, for the
acceptance timer, by `rova/ordering/checkout.py` right after order
creation).

Item-2 fix (backend-review-r1.md): `AUTO_ACCEPT` (SYSTEM, R-128) added so the
`acceptance_sla` job can accept an `AUTO_ACCEPT_FULL` vendor's order without
misusing `ACCEPTANCE_TIMEOUT` (which is `MANUAL_CONFIRM`-only, per A4.2's own
table). `COMPLIANCE_HALT` (R-121) and `NOTHING_TO_DISPATCH` added — the
latter fired by `maybe_fire_nothing_to_dispatch` below, called from SM-04's
licence-lapse/compliance-halt cascades and from the reroute-decision
endpoint, whenever an order's lines all end up at zero confirmed quantity
after acceptance. `DISPATCH`'s guard now also checks batch/lot/expiry, not
only seal_ids (R-054's full text), and its effects now write
`traceability_event(DISPATCHED)` per seal and create the `delivery_job`
(A8.6) — previously done, wrongly, by an invented `mark-delivered` route and
a raw `order_line` UPDATE in the router. `CLOSE`'s guard now enforces R-126
(return window elapsed, a price-matched invoice, and either it is
settled or the order was on credit terms)."""
from datetime import timedelta

from sqlalchemy import text

from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.ids import new_id
from rova.domain import timers
from rova.domain.enums import OrderStatus, RoleCode
from rova.domain.fsm import Ctx, GuardResult, Machine, Transition

_VENDOR_DESK = frozenset({RoleCode.VENDOR_ORDER_DESK, RoleCode.VENDOR_ADMIN})
_VENDOR_PICKER = frozenset({RoleCode.VENDOR_PICKER, RoleCode.VENDOR_ORDER_DESK, RoleCode.VENDOR_ADMIN})
_PHARMACY = frozenset({RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER})
_RECEIVER = frozenset({RoleCode.PHARMACY_RECEIVER, RoleCode.PHARMACY_ADMIN})
_COMPLIANCE = frozenset({RoleCode.COMPLIANCE_OFFICER})


def _guard_dispatch(ctx: Ctx) -> GuardResult:
    """R-054: every line with confirmed_qty > 0 must carry >= 1 seal, a
    batch number, a lot number and an expiry date. R-121: no OrderLine may
    still be LINE_SHORT (an undecided reroute-ask) when dispatching — a
    short line's remainder is only resolved once it becomes LINE_REROUTED or
    LINE_DROPPED (sm04_order_line.py); dispatching while it is still
    LINE_SHORT and undecided lets the REROUTE_ASK timer DROP a line out from
    under an order that already shipped."""
    rows = ctx.session.execute(
        text("SELECT confirmed_qty, seal_ids, batch_number, lot_number, expiry_date, fulfilment_status "
             "FROM order_line WHERE order_id=:o"),
        {"o": ctx.subject_id},
    ).mappings().all()
    for row in rows:
        if row["fulfilment_status"] == "LINE_SHORT":
            return GuardResult.failed(
                "an order line is still LINE_SHORT (undecided reroute/drop/substitute); "
                "dispatch is blocked until it is resolved", rule="R-121"
            )
        if row["confirmed_qty"] > 0 and not (
            row["seal_ids"] and row["batch_number"] and row["lot_number"] and row["expiry_date"]
        ):
            return GuardResult.failed(
                "every dispatched line needs at least one seal, batch, lot and expiry date", rule="R-054"
            )
    return GuardResult.passed()


def _guard_close(ctx: Ctx) -> GuardResult:
    """R-126: return window elapsed since RECEIPT_ACCEPTED AND a
    price-matched invoice exists AND (it is PAID/WRITTEN_OFF, or the order
    ran on CREDIT_N_DAYS terms — credit terms confirmed, collection is the
    credit facility's business, not a blocker on closing the order)."""
    session = ctx.session
    receipt_at = session.execute(
        text(
            "SELECT occurred_at FROM state_transition WHERE machine='SM-03' AND subject_id=:o "
            "AND to_state='RECEIPT_ACCEPTED' ORDER BY occurred_at DESC LIMIT 1"
        ),
        {"o": ctx.subject_id},
    ).scalar()
    if receipt_at is None:
        return GuardResult.failed("order was never RECEIPT_ACCEPTED", rule="R-126")
    days = cfg.get(session, "CFG-RETURN-WINDOW-DAYS", default=7)
    if now() < receipt_at + timedelta(days=float(days)):
        return GuardResult.failed("the return window has not elapsed yet", rule="R-126")
    invoice = session.execute(
        text(
            "SELECT status FROM invoice WHERE order_id=:o AND price_match_flag='OK' "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"o": ctx.subject_id},
    ).mappings().first()
    if invoice is None:
        return GuardResult.failed("no invoice with price_match_flag = 'OK' exists for this order", rule="R-126")
    if invoice["status"] in ("PAID", "WRITTEN_OFF"):
        return GuardResult.passed()
    if ctx.subject_row["payment_terms"] == "CREDIT_N_DAYS":
        return GuardResult.passed()
    return GuardResult.failed("invoice is not settled and the order was not on credit terms", rule="R-126")


def _extra_set_accept(ctx: Ctx) -> dict:
    now_ts = now()
    promised = ctx.kwargs.get("promised_dispatch_at")
    if promised is None:
        vendor_id = ctx.subject_row["vendor_id"]
        acceptance_mode = ctx.session.execute(
            text("SELECT acceptance_mode FROM vendor_account WHERE id=:v"), {"v": vendor_id}
        ).scalar()
        if acceptance_mode == "AUTO_ACCEPT_FULL":
            hours = cfg.get(ctx.session, "CFG-SLA-DISPATCH-HOURS", vendor_id=vendor_id, default=24)
            promised = now_ts + timedelta(hours=float(hours))
    return {"accepted_at": now_ts, "promised_dispatch_at": promised}


def _effect_accept(ctx: Ctx) -> None:
    timers.cancel(ctx.session, policy_type="ACCEPTANCE", subject_type="order", subject_id=ctx.subject_id)
    timers.start(ctx.session, policy_type="DISPATCH", subject_type="order", subject_id=ctx.subject_id,
                 cfg_key="CFG-SLA-DISPATCH-HOURS", vendor_id=ctx.subject_row["vendor_id"])


def _effect_dispatch(ctx: Ctx) -> None:
    session = ctx.session
    timers.cancel(session, policy_type="DISPATCH", subject_type="order", subject_id=ctx.subject_id)
    order = ctx.subject_row
    lines = session.execute(
        text("SELECT * FROM order_line WHERE order_id=:o AND confirmed_qty > 0"), {"o": ctx.subject_id}
    ).mappings().all()
    for line in lines:
        for seal in line["seal_ids"]:
            session.execute(
                text(
                    "INSERT INTO traceability_event (id, order_line_id, seal_id, batch_number, lot_number, "
                    "expiry_date, event_type, actor_user_id, actor_role) VALUES "
                    "(:id, :ol, :seal, :b, :l, :e, 'DISPATCHED', :uid, :role)"
                ),
                {"id": new_id("trc"), "ol": line["id"], "seal": seal, "b": line["batch_number"],
                 "l": line["lot_number"], "e": line["expiry_date"], "uid": ctx.actor_user_id,
                 "role": ctx.actor_role},
            )
    session.execute(
        text(
            "INSERT INTO delivery_job (id, order_id, vendor_id, pharmacy_id, performed_by, status) VALUES "
            "(:id, :o, :v, :p, :pb, 'CREATED')"
        ),
        {"id": new_id("dlv"), "o": ctx.subject_id, "v": order["vendor_id"], "p": order["pharmacy_id"],
         "pb": order["delivery_mode"]},
    )


def _effect_pharmacy_cancel_before_acceptance(ctx: Ctx) -> None:
    timers.cancel(ctx.session, policy_type="ACCEPTANCE", subject_type="order", subject_id=ctx.subject_id)


def _create_dispute(ctx: Ctx, dispute_type: str, receipt_line_id: str | None) -> None:
    session = ctx.session
    raised_by = "SYSTEM" if ctx.actor == "SYSTEM" else ctx.actor_role
    session.execute(
        text(
            "INSERT INTO dispute (id, order_id, receipt_line_id, type, raised_by, status) VALUES "
            "(:id, :o, :rl, :t, :rb, 'OPEN')"
        ),
        {"id": new_id("dsp"), "o": ctx.subject_id, "rl": receipt_line_id,
         "t": dispute_type, "rb": raised_by},
    )


def _effect_receipt_dispute(ctx: Ctx) -> None:
    """R-072: one dispute(RECEIPT_DISCREPANCY) per rejected receipt_line —
    `receipt_line_ids` (plural) is the list of receipt_line ids with
    rejected_qty > 0, collected by the receipt endpoint before it decides
    ACCEPT vs DISPUTE."""
    ids = ctx.kwargs.get("receipt_line_ids") or (
        [ctx.kwargs["receipt_line_id"]] if ctx.kwargs.get("receipt_line_id") else []
    )
    dispute_type = ctx.kwargs.get("dispute_type", "RECEIPT_DISCREPANCY")
    for rl_id in ids:
        _create_dispute(ctx, dispute_type, rl_id)


def _create_return(ctx: Ctx, origin: str) -> None:
    session = ctx.session
    seq = session.execute(text("SELECT nextval('rma_number_seq')")).scalar()
    session.execute(
        text(
            "INSERT INTO \"return\" (id, order_id, rma_number, status, origin) VALUES "
            "(:id, :o, :rma, 'REQUESTED', :origin)"
        ),
        {"id": new_id("rtn"), "o": ctx.subject_id, "rma": f"RMA-{now().year}-{seq:06d}", "origin": origin},
    )


def _effect_dispute_resolved_return(ctx: Ctx) -> None:
    _create_return(ctx, "DISPUTE_RESOLUTION")


def _effect_rma_requested(ctx: Ctx) -> None:
    _create_return(ctx, "PHARMACY_RMA")


def _effect_compliance_halt(ctx: Ctx) -> None:
    """§6 ordering requires the SM-04 line effects to run *before* the order
    write, but the engine's `apply()` always writes the subject row first and
    only then runs `effects` (fsm.py Machine.apply) — an effect on SM-03
    cannot reorder the SM-03 UPDATE that already ran to get here. So the
    SM-04 cascade over this order's LINE_FULL lines is no longer done here:
    it is done by the `/v1/orders/{id}/compliance-halt` router handler
    *before* it calls `MACHINES["SM-03"].apply(..., "COMPLIANCE_HALT", ...)`
    at all, which is the only way to make the line `order_line`
    UPDATE/`state_transition` rows actually precede the order's `CANCELLED`
    UPDATE/`state_transition` rows in the transaction. This effect now only
    does the parts that have no ordering constraint against the lines:
    cancel whatever timers this order still held."""
    session = ctx.session
    timers.cancel(session, policy_type="ACCEPTANCE", subject_type="order", subject_id=ctx.subject_id)
    timers.cancel(session, policy_type="DISPATCH", subject_type="order", subject_id=ctx.subject_id)
    session.execute(
        text(
            "INSERT INTO audit_event (id, actor_user_id, actor_role, action_code, subject_type, subject_id, "
            "rule_ref, metadata) VALUES (:id, :uid, :role, 'COMPLIANCE_HALT', 'order', :sid, 'R-121', "
            "CAST(:meta AS JSONB))"
        ),
        {"id": new_id("aud"), "uid": ctx.actor_user_id, "role": ctx.actor_role, "sid": ctx.subject_id,
         "meta": '{"reason": "%s"}' % ctx.kwargs.get("reason")},
    )


# Public: the compliance-halt router validates against this same set before
# it starts the SM-04 line cascade (see _effect_compliance_halt's docstring
# for why that cascade had to move out of this module's effect).
COMPLIANCE_HALT_REASONS = ("REGULATED_PRICE_INCIDENT", "COUNTERFEIT_SUSPECTED", "LICENCE_REVOKED")


def _guard_compliance_halt(ctx: Ctx) -> GuardResult:
    if ctx.kwargs.get("reason") not in COMPLIANCE_HALT_REASONS:
        return GuardResult.failed(f"reason must be one of {COMPLIANCE_HALT_REASONS}", rule="R-121")
    return GuardResult.passed()


def _guard_nothing_to_dispatch(ctx: Ctx) -> GuardResult:
    total = ctx.session.execute(
        text("SELECT COALESCE(SUM(confirmed_qty),0) FROM order_line WHERE order_id=:o"), {"o": ctx.subject_id}
    ).scalar()
    if total != 0:
        return GuardResult.failed("some order_line still carries a confirmed_qty > 0")
    return GuardResult.passed()


MACHINE = Machine(
    code="SM-03",
    subject_table="order",
    status_column="status",
    transitions=[
        Transition("SM-03", (OrderStatus.PENDING_ACCEPTANCE,), OrderStatus.ACCEPTED, "ACCEPT",
                   _VENDOR_DESK, effects=_effect_accept, extra_set=_extra_set_accept),
        Transition("SM-03", (OrderStatus.PENDING_ACCEPTANCE,), OrderStatus.ACCEPTED, "AUTO_ACCEPT",
                   frozenset({"SYSTEM"}), effects=_effect_accept, extra_set=_extra_set_accept,
                   rule_refs=("R-128",)),
        Transition("SM-03", (OrderStatus.PENDING_ACCEPTANCE,), OrderStatus.REJECTED, "REJECT", _VENDOR_DESK),
        Transition("SM-03", (OrderStatus.PENDING_ACCEPTANCE,), OrderStatus.CANCELLED, "PHARMACY_CANCEL",
                   _PHARMACY, effects=_effect_pharmacy_cancel_before_acceptance,
                   extra_set=lambda ctx: {"cancel_reason": "PHARMACY"}),
        Transition("SM-03", (OrderStatus.PENDING_ACCEPTANCE,), OrderStatus.CANCELLED, "ACCEPTANCE_TIMEOUT",
                   frozenset({"SYSTEM"}), extra_set=lambda ctx: {"cancel_reason": "ACCEPTANCE_TIMEOUT"}),
        Transition("SM-03", (OrderStatus.PENDING_ACCEPTANCE, OrderStatus.ACCEPTED), OrderStatus.CANCELLED,
                   "COMPLIANCE_HALT", _COMPLIANCE, guard=_guard_compliance_halt, effects=_effect_compliance_halt,
                   extra_set=lambda ctx: {"cancel_reason": ctx.kwargs.get("reason"), "compliance_flag": True},
                   rule_refs=("R-121",)),
        Transition("SM-03", (OrderStatus.ACCEPTED,), OrderStatus.CANCELLED, "NOTHING_TO_DISPATCH",
                   frozenset({"SYSTEM"}), guard=_guard_nothing_to_dispatch,
                   extra_set=lambda ctx: {"cancel_reason": "NOTHING_TO_DISPATCH"}),
        Transition("SM-03", (OrderStatus.ACCEPTED,), OrderStatus.DISPATCHED, "DISPATCH",
                   _VENDOR_PICKER, guard=_guard_dispatch, effects=_effect_dispatch,
                   extra_set=lambda ctx: {"dispatched_at": now()}, rule_refs=("R-054", "R-121")),
        Transition("SM-03", (OrderStatus.ACCEPTED,), OrderStatus.CANCELLED, "DISPATCH_TIMEOUT",
                   frozenset({"SYSTEM"}), extra_set=lambda ctx: {"cancel_reason": "DISPATCH_TIMEOUT"}),
        Transition("SM-03", (OrderStatus.DISPATCHED,), OrderStatus.DELIVERED_PENDING_RECEIPT, "DELIVERED",
                   frozenset({"SYSTEM", RoleCode.COURIER, RoleCode.DISPATCHER})),
        Transition("SM-03", (OrderStatus.DISPATCHED,), OrderStatus.CANCELLED, "DELIVERY_EXHAUSTED",
                   frozenset({"SYSTEM"}), extra_set=lambda ctx: {"cancel_reason": "DELIVERY_EXHAUSTED"}),
        Transition("SM-03", (OrderStatus.DELIVERED_PENDING_RECEIPT,), OrderStatus.RECEIPT_ACCEPTED,
                   "RECEIPT_ACCEPT", _RECEIVER),
        Transition("SM-03", (OrderStatus.DELIVERED_PENDING_RECEIPT,), OrderStatus.DISPUTED, "RECEIPT_DISPUTE",
                   _RECEIVER, effects=_effect_receipt_dispute),
        Transition("SM-03", (OrderStatus.DISPUTED,), OrderStatus.RECEIPT_ACCEPTED, "DISPUTE_RESOLVED_ACCEPT",
                   frozenset({RoleCode.COMPLIANCE_OFFICER, RoleCode.VENDOR_FINANCE})),
        Transition("SM-03", (OrderStatus.DISPUTED,), OrderStatus.RETURN_IN_PROGRESS, "DISPUTE_RESOLVED_RETURN",
                   frozenset({RoleCode.VENDOR_FINANCE}), effects=_effect_dispute_resolved_return),
        Transition("SM-03", (OrderStatus.RECEIPT_ACCEPTED,), OrderStatus.RETURN_IN_PROGRESS, "RMA_REQUESTED",
                   frozenset({RoleCode.PHARMACY_ADMIN}), effects=_effect_rma_requested),
        Transition("SM-03", (OrderStatus.RETURN_IN_PROGRESS,), OrderStatus.CLOSED, "RETURN_CLOSED",
                   frozenset({"SYSTEM"})),
        Transition("SM-03", (OrderStatus.RECEIPT_ACCEPTED,), OrderStatus.CLOSED, "CLOSE",
                   frozenset({"SYSTEM", RoleCode.PLATFORM_ADMIN}), guard=_guard_close, rule_refs=("R-126",)),
    ],
)


def maybe_fire_nothing_to_dispatch(session, order_id: str) -> None:
    """Called after any SM-04 effect that can zero out an order's last
    confirmed quantity post-acceptance (licence lapse, compliance halt, a
    pharmacy DROP decision) — fires SM-03 NOTHING_TO_DISPATCH so the order
    does not sit ACCEPTED forever with nothing left to dispatch. A no-op
    (via GUARD_FAILED, swallowed) when the order is not ACCEPTED or still
    has a line with confirmed_qty > 0."""
    from rova.core.errors import ApiError
    order = session.execute(text('SELECT status FROM "order" WHERE id=:o'), {"o": order_id}).mappings().first()
    if order is None or order["status"] != OrderStatus.ACCEPTED:
        return
    try:
        MACHINE.apply(session, order_id, "NOTHING_TO_DISPATCH", "SYSTEM")
    except ApiError:
        pass
