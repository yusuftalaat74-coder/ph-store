"""Item-2 fix (backend-review-r1.md): nothing ever fired SM-01
`ALL_ORDERS_TERMINAL`, so a request whose only order got cancelled by
`ACCEPTANCE_TIMEOUT`/`DISPATCH_TIMEOUT`/etc. was stranded in `IN_FULFILMENT`
forever. Registered in `rova/domain/hooks.py` against every SM-03 terminal
`to_state` (`CANCELLED`, `REJECTED`, `CLOSED`)."""
from sqlalchemy import text

from rova.domain.fsm import Ctx

_ORDER_TERMINAL_STATES = ("CANCELLED", "REJECTED", "CLOSED")


def on_order_terminal_close_request(ctx: Ctx) -> None:
    session = ctx.session
    request_id = ctx.subject_row["request_id"]
    request = session.execute(
        text("SELECT status FROM request WHERE id=:r"), {"r": request_id}
    ).mappings().first()
    if request is None or request["status"] != "IN_FULFILMENT":
        return
    still_open = session.execute(
        text('SELECT 1 FROM "order" WHERE request_id=:r AND status <> ALL(:terminal)'),
        {"r": request_id, "terminal": list(_ORDER_TERMINAL_STATES)},
    ).first()
    if still_open is not None:
        return
    from rova.domain.machines.registry import MACHINES
    MACHINES["SM-01"].apply(session, request_id, "ALL_ORDERS_TERMINAL", "SYSTEM")
