"""Cross-cutting hooks registered against (machine, to_state), so that a
machine module in rova/domain/machines/ never imports rova.fees / rova.eta /
rova.notifications / rova.vendor_score directly (A4.1). Each implemented
service registers its own hooks here at import time via `wire()`."""
from rova.domain.fsm import register_hook

_wired = False


def wire() -> None:
    """Called from rova.main:create_app() and from the CLI entry points that
    apply state-machine transitions (seed, jobs). Importing the hook-owning
    modules here (rather than at module load time of fsm.py) is what keeps
    machine modules free of fee/notification imports. Idempotent — safe to
    call more than once per process."""
    global _wired
    if _wired:
        return
    from rova.fees import accrual as fees_accrual
    from rova.eta import service as eta_service
    from rova.ordering import cart as cart_service
    from rova.ordering import request_lifecycle

    register_hook("SM-03", "RECEIPT_ACCEPTED", fees_accrual.on_receipt_accepted)
    register_hook("SM-01", "CONFIRMED", fees_accrual.on_request_confirmed)

    # SM-20 (E-068, R-158): the order's open eta_estimate tracks SM-03/SM-05.
    register_hook("SM-03", "ACCEPTED", eta_service.on_order_accepted)
    register_hook("SM-03", "DISPATCHED", eta_service.on_order_dispatched)
    register_hook("SM-03", "CANCELLED", eta_service.on_order_terminal)
    register_hook("SM-03", "REJECTED", eta_service.on_order_terminal)
    # Defect 2 fix: SM-20 GO_LIVE (COMMITTED -> IN_TRANSIT_LIVE) is never
    # fired in v1 (backend-spec.md A4.2 row SM-20: "GO_LIVE (courier pings —
    # never fires in v1, A18)"). It used to fire here off SM-05's ASSIGNED ->
    # IN_TRANSIT, which is illegal on a second START after
    # ATTEMPT_FAILED_RETRY -> RESCHEDULE -> ASSIGNED (the estimate is already
    # past COMMITTED by then it stayed IN_TRANSIT_LIVE, and GO_LIVE is only
    # legal from COMMITTED) — stranding the delivery job in ASSIGNED with a
    # 409 ILLEGAL_TRANSITION on every retried START. eta_service.py's
    # COMMITTED -> REALISED/MISSED transitions (the spec's own documented v1
    # deviation, since IN_TRANSIT_LIVE is unreachable without real courier
    # pings) already cover DELIVERED without this hook.
    register_hook("SM-05", "DELIVERED", eta_service.on_delivery_delivered)

    # SM-01 ALL_ORDERS_TERMINAL (item 2, backend-review-r1.md): every order
    # terminal state must re-check whether the parent request can close.
    register_hook("SM-03", "CANCELLED", request_lifecycle.on_order_terminal_close_request)
    register_hook("SM-03", "REJECTED", request_lifecycle.on_order_terminal_close_request)
    register_hook("SM-03", "CLOSED", request_lifecycle.on_order_terminal_close_request)

    # A cart that is no longer a cart must stop being advertised as one. Every
    # SM-01 state DRAFT can leave for is here, so no route out of the basket
    # can leave an idle-cart notice pointing at it.
    for left_draft in ("CONFIRMED", "AWAITING_ADMIN_APPROVAL", "CANCELLED"):
        register_hook("SM-01", left_draft, cart_service.on_cart_left_draft)
    _wired = True
