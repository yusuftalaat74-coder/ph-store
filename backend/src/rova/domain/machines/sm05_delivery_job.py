"""SM-05 — delivery_job (§6 SM-05, A4.2 row SM-05).

Implemented subset: `ASSIGN`, `START`, three `ATTEMPT` outcomes split into
distinct triggers (`ATTEMPT_DELIVERED` / `ATTEMPT_FAILED_RETRY` /
`ATTEMPT_FAILED_FINAL`) because the engine's `Transition.to_state` is fixed
per trigger (A4.1) and the spec's single `ATTEMPT` trigger branches on the
attempt outcome and `attempt_count` vs `CFG-DELIVERY-MAX-ATTEMPTS` — the
same split already used by the previous executor's SM-03 `AUTO_ACCEPT` vs
manual `ACCEPT`. `attempt_count` is incremented in the same `UPDATE` as the
status write (`extra_set`), before the guard on the two `FAILED_*` variants
per §6 ("attempt_count is incremented immediately ... before evaluating any
guard"). `RESCHEDULE`, `EXHAUST_WHILE_PENDING`, `PARENT_CANCELLED` complete
the table; `DELIVERED`/`DELIVERY_EXHAUSTED` fire the matching SM-03
transition as an effect."""
from sqlalchemy import text

from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.ids import new_id
from rova.domain.enums import DeliveryJobStatus, RoleCode
from rova.domain.fsm import Ctx, GuardResult, Machine, Transition

_DISPATCHER = frozenset({RoleCode.DISPATCHER})
# A15.4 row 126: `/v1/delivery-jobs/{id}/assign` is "DI VA own" (Dispatcher OR
# VendorAdmin of the owning vendor) — the pre-existing actor set here only
# had Dispatcher, so a VENDOR_OWN_FLEET vendor with no separate Dispatcher
# membership (VendorAdmin doing everything, a real shape per A9.1) could
# never assign its own delivery jobs even though `fulfilment/router.py`
# already allowed the HTTP call through; the engine then rejected it as
# FORBIDDEN. VENDOR_ADMIN added to match the spec row exactly.
_ASSIGN_ACTORS = frozenset({RoleCode.DISPATCHER, RoleCode.VENDOR_ADMIN})
_COURIER = frozenset({RoleCode.COURIER})

SUBJECT = "delivery_job"


def _next_attempt_count(ctx: Ctx) -> int:
    return ctx.subject_row["attempt_count"] + 1


def _guard_retry_pending(ctx: Ctx) -> GuardResult:
    max_attempts = cfg.get(ctx.session, "CFG-DELIVERY-MAX-ATTEMPTS", default=3)
    if _next_attempt_count(ctx) >= max_attempts:
        return GuardResult.failed("attempt_count reached CFG-DELIVERY-MAX-ATTEMPTS; must return to vendor")
    return GuardResult.passed()


def _guard_final(ctx: Ctx) -> GuardResult:
    max_attempts = cfg.get(ctx.session, "CFG-DELIVERY-MAX-ATTEMPTS", default=3)
    if _next_attempt_count(ctx) < max_attempts:
        return GuardResult.failed("attempt_count has not reached CFG-DELIVERY-MAX-ATTEMPTS yet")
    return GuardResult.passed()


def _guard_exhaust_while_pending(ctx: Ctx) -> GuardResult:
    """No new attempt is recorded here (§6): attempt_count already sits at
    the max from the last failed attempt while the job waited for
    reassignment."""
    max_attempts = cfg.get(ctx.session, "CFG-DELIVERY-MAX-ATTEMPTS", default=3)
    if ctx.subject_row["attempt_count"] < max_attempts:
        return GuardResult.failed("attempt_count has not reached CFG-DELIVERY-MAX-ATTEMPTS yet")
    return GuardResult.passed()


def _create_return(ctx: Ctx) -> None:
    session = ctx.session
    order_id = ctx.subject_row["order_id"]
    seq = session.execute(text("SELECT nextval('rma_number_seq')")).scalar()
    rma_number = f"RMA-{now().year}-{seq:06d}"
    session.execute(
        text(
            "INSERT INTO \"return\" (id, order_id, rma_number, status, origin) VALUES "
            "(:id, :o, :rma, 'REQUESTED', 'DELIVERY_EXHAUSTED')"
        ),
        {"id": new_id("rtn"), "o": order_id, "rma": rma_number},
    )


def _effect_delivered(ctx: Ctx) -> None:
    from rova.domain.machines.registry import MACHINES
    MACHINES["SM-03"].apply(ctx.session, ctx.subject_row["order_id"], "DELIVERED", "SYSTEM")


def _effect_exhausted(ctx: Ctx) -> None:
    from rova.domain.machines.registry import MACHINES
    MACHINES["SM-03"].apply(ctx.session, ctx.subject_row["order_id"], "DELIVERY_EXHAUSTED", "SYSTEM")
    _create_return(ctx)


def _guard_parent_cancelled(ctx: Ctx) -> GuardResult:
    status = ctx.session.execute(
        text('SELECT status FROM "order" WHERE id=:o'), {"o": ctx.subject_row["order_id"]}
    ).scalar()
    if status != "CANCELLED":
        return GuardResult.failed("parent order is not CANCELLED")
    return GuardResult.passed()


MACHINE = Machine(
    code="SM-05",
    subject_table=SUBJECT,
    status_column="status",
    transitions=[
        Transition("SM-05", (DeliveryJobStatus.CREATED,), DeliveryJobStatus.ASSIGNED, "ASSIGN", _ASSIGN_ACTORS,
                   extra_set=lambda ctx: {
                       "transporter_id": ctx.kwargs.get("transporter_id"),
                       "courier_user_id": ctx.kwargs.get("courier_user_id"),
                   }),
        Transition("SM-05", (DeliveryJobStatus.ASSIGNED,), DeliveryJobStatus.IN_TRANSIT, "START", _COURIER),
        Transition("SM-05", (DeliveryJobStatus.IN_TRANSIT,), DeliveryJobStatus.DELIVERED, "ATTEMPT_DELIVERED",
                   _COURIER, effects=_effect_delivered,
                   extra_set=lambda ctx: {"attempt_count": _next_attempt_count(ctx)}),
        Transition("SM-05", (DeliveryJobStatus.IN_TRANSIT,), DeliveryJobStatus.FAILED_RETRY_PENDING,
                   "ATTEMPT_FAILED_RETRY", _COURIER, guard=_guard_retry_pending,
                   extra_set=lambda ctx: {"attempt_count": _next_attempt_count(ctx)}),
        Transition("SM-05", (DeliveryJobStatus.IN_TRANSIT,), DeliveryJobStatus.RETURNED_TO_VENDOR,
                   "ATTEMPT_FAILED_FINAL", _COURIER, guard=_guard_final, effects=_effect_exhausted,
                   extra_set=lambda ctx: {"attempt_count": _next_attempt_count(ctx)}),
        Transition("SM-05", (DeliveryJobStatus.FAILED_RETRY_PENDING,), DeliveryJobStatus.ASSIGNED, "RESCHEDULE",
                   _DISPATCHER),
        Transition("SM-05", (DeliveryJobStatus.FAILED_RETRY_PENDING,), DeliveryJobStatus.RETURNED_TO_VENDOR,
                   "EXHAUST_WHILE_PENDING", frozenset({"SYSTEM"}), guard=_guard_exhaust_while_pending,
                   effects=_effect_exhausted),
        Transition("SM-05", (DeliveryJobStatus.CREATED, DeliveryJobStatus.ASSIGNED), DeliveryJobStatus.CANCELLED,
                   "PARENT_CANCELLED", frozenset({"SYSTEM"}), guard=_guard_parent_cancelled),
    ],
)
