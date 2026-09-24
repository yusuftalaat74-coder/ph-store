"""wire_office_hooks(): FSM hooks that only WRITE TO THE OUTBOX (SPEC 5.9.3).
Called from rova.domain.hooks.wire(); every hook is a no-op while the link is
off (outbox.enqueue checks the flag), so registering them changes nothing."""
from sqlalchemy import text

from rova.domain.fsm import Ctx, register_hook
from rova.integrations.office import outbox, payloads

_wired = False


def _order(event_type, builder):
    def hook(ctx: Ctx) -> None:
        outbox.enqueue(ctx.session, event_type, "order", ctx.subject_id, builder(ctx.session, ctx.subject_id))
    hook.__name__ = f"office_{event_type.replace('.', '_')}"
    return hook


def _dispute_resolved(ctx: Ctx) -> None:
    order_id = ctx.subject_row["order_id"]
    outbox.enqueue(ctx.session, "order.dispute_resolved", "order", order_id,
                   payloads.build_dispute_resolved(ctx.session, ctx.subject_id),
                   aggregate_version=outbox.state_version(ctx.session, order_id))


def _return(event_type):
    def hook(ctx: Ctx) -> None:
        outbox.enqueue(ctx.session, event_type, "return", ctx.subject_id, payloads.build_return(ctx.session, ctx.subject_id))
    return hook


def _pharmacy(ctx: Ctx) -> None:
    to_state = ctx.subject_row["status"]
    from_state = ctx.session.execute(
        text("SELECT from_state FROM state_transition WHERE machine='SM-07' AND subject_id=:s "
             "ORDER BY occurred_at DESC, id DESC LIMIT 1"), {"s": ctx.subject_id},
    ).scalar()
    event_type = {"SUSPENDED": "pharmacy.suspended", "CLOSED": "pharmacy.closed"}.get(to_state)
    if to_state == "ACTIVE":
        event_type = "pharmacy.reinstated" if from_state == "SUSPENDED" else "pharmacy.approved"
    if event_type is None:
        return
    outbox.enqueue(ctx.session, event_type, "pharmacy", ctx.subject_id, payloads.build_pharmacy(ctx.session, ctx.subject_id))


def fee_accrued(session, fee_event_id: str) -> None:
    """Called by rova/fees/accrual.py right after each fee_event INSERT."""
    outbox.enqueue(session, "fee.accrued", "fee_event", fee_event_id, payloads.build_fee(session, fee_event_id),
                   aggregate_version=0)


def fee_reversed(session, fee_event_id: str) -> None:
    """For the fee-reversal path (no call site in PH Store yet — see docs)."""
    outbox.enqueue(session, "fee.reversed", "fee_event", fee_event_id, payloads.build_fee(session, fee_event_id),
                   aggregate_version=1)


def platform_invoice(session, platform_invoice_id: str, settled: bool) -> None:
    """For platform_invoice ISSUED/SETTLED (no call site in PH Store yet — see docs)."""
    outbox.enqueue(session, "platform_invoice.settled" if settled else "platform_invoice.issued", "platform_invoice",
                   platform_invoice_id, payloads.build_platform_invoice(session, platform_invoice_id),
                   aggregate_version=2 if settled else 1)


def credit_facility_created(session, facility_id: str) -> None:
    outbox.enqueue(session, "credit_facility.created", "credit_facility", facility_id,
                   payloads.build_credit_facility(session, facility_id), aggregate_version=0)


def order_created(session, order_id: str) -> None:
    """Called explicitly from checkout (creation is an INSERT, not a transition)."""
    outbox.enqueue(session, "order.created", "order", order_id, payloads.build_order_created(session, order_id),
                   aggregate_version=0)


def wire_office_hooks() -> None:
    global _wired
    if _wired:
        return
    register_hook("SM-03", "CANCELLED", _order("order.cancelled", payloads.build_order_cancelled))
    register_hook("SM-03", "RECEIPT_ACCEPTED", _order("order.receipt_accepted", payloads.build_receipt))
    register_hook("SM-03", "DISPUTED", _order("order.disputed", payloads.build_disputed))
    register_hook("SM-03", "CLOSED", _order("order.closed", lambda s, o: {"order_id": o}))
    register_hook("SM-09", "RESOLVED", _dispute_resolved)
    register_hook("SM-10", "APPROVED", _return("return.approved"))
    register_hook("SM-10", "REJECTED", _return("return.rejected"))
    for state in ("ACTIVE", "SUSPENDED", "CLOSED"):
        register_hook("SM-07", state, _pharmacy)
    _wired = True
