"""A13.1 — cross-cutting hooks that drive SM-20 off SM-03/SM-05 transitions
(A4.1: a machine module never imports another machine directly; this module
is the one place allowed to import both `rova.domain.machines.sm20_eta_estimate`
and be registered against `(SM-03|SM-05, to_state)` in `rova/domain/hooks.py`).
Every call is a no-op when the order has no open estimate (e.g. it was
created before this hook was wired, or already VOIDed) rather than raising —
ETA is an observability layer, not a blocker on the order/delivery machines.

Defect 2 fix: there is deliberately no `on_delivery_in_transit`/`GO_LIVE`
hook here. `GO_LIVE` (`COMMITTED -> IN_TRANSIT_LIVE`) never fires in v1
(backend-spec.md A4.2 row SM-20, A18 — no real courier-location ping in this
backend); firing it off SM-05's `ASSIGNED -> IN_TRANSIT` made every courier
`START` after a failed-then-rescheduled delivery attempt raise 409
`ILLEGAL_TRANSITION` (`GO_LIVE` is only legal from `COMMITTED`, and the
estimate was already past it), permanently stranding the order. `MACHINE`'s
own `COMMITTED -> REALISED`/`COMMITTED -> MISSED` transitions in
`sm20_eta_estimate.py` are the spec's documented v1 deviation for exactly
this reason and are all `on_delivery_delivered` below needs."""
from sqlalchemy import text

from rova.domain.fsm import Ctx
from rova.domain.machines.sm20_eta_estimate import open_estimate_id


def _apply(ctx: Ctx, order_id: str, trigger: str, **kwargs) -> None:
    from rova.domain.machines.registry import MACHINES
    eta_id = open_estimate_id(ctx.session, order_id)
    if eta_id is None:
        return
    MACHINES["SM-20"].apply(ctx.session, eta_id, trigger, "SYSTEM", **kwargs)


def on_order_accepted(ctx: Ctx) -> None:
    """SM-03 -> ACCEPTED: PROVISIONAL -> COMMITTED."""
    _apply(ctx, ctx.subject_id, "COMMIT")


def on_order_dispatched(ctx: Ctx) -> None:
    """SM-03 -> DISPATCHED: COMMITTED -> COMMITTED (narrows to transit-only)."""
    _apply(ctx, ctx.subject_id, "NARROW_ON_DISPATCH")


def on_order_terminal(ctx: Ctx) -> None:
    """SM-03 -> CANCELLED|REJECTED: any open estimate -> VOID."""
    _apply(ctx, ctx.subject_id, "VOID")


def on_delivery_delivered(ctx: Ctx) -> None:
    """SM-05 -> DELIVERED: REALISE or MISS depending on whether `now()` (the
    delivery moment) falls inside the estimate's range — chosen here, before
    calling `apply`, the same way `rova/billing/invoices.py` chooses
    MATCH_OK/MATCH_DEVIATION for SM-11 (A4.1's fixed-`to_state`-per-trigger
    constraint)."""
    from rova.core.clock import now
    order_id = ctx.subject_row["order_id"]
    eta_id = open_estimate_id(ctx.session, order_id)
    if eta_id is None:
        return
    latest_at = ctx.session.execute(
        text("SELECT latest_at FROM eta_estimate WHERE id=:id"), {"id": eta_id}
    ).scalar()
    realised_at = now()
    trigger = "REALISE" if realised_at <= latest_at else "MISS"
    _apply(ctx, order_id, trigger, realised_at=realised_at)
