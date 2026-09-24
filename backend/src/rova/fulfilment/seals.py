"""R-057 — a seal is dispatched once. Shared by the vendor `pick` route and
the PH Office `order.picked` applier (REVIEW F-5), so both refuse a reused
seal with a 4xx naming it instead of a bare IntegrityError at DISPATCH.

`uq_traceability_seal_dispatched` only fires when SM-03 DISPATCH inserts the
traceability_event(DISPATCHED) rows; this checks, at pick time, both seals
already DISPATCHED (the constraint itself) and seals already sitting on
another line of the same order (which would otherwise fail inside that
order's own single DISPATCH transaction)."""
from sqlalchemy import text

from rova.core.errors import ApiError


def seal_dupes(session, order_id: str, line_id: str, seal_ids: list[str]) -> list[str]:
    dispatched = session.execute(
        text("SELECT DISTINCT seal_id FROM traceability_event WHERE event_type='DISPATCHED' "
             "AND seal_id = ANY(:seals)"),
        {"seals": seal_ids},
    ).scalars().all()
    picked = session.execute(
        text("SELECT DISTINCT unnest(seal_ids) AS seal_id FROM order_line "
             "WHERE order_id=:o AND id <> :line AND seal_ids && :seals"),
        {"o": order_id, "line": line_id, "seals": seal_ids},
    ).scalars().all()
    return sorted((set(dispatched) | set(picked)) & set(seal_ids))


def check_seals_unique(session, order_id: str, line_id: str, seal_ids: list[str]) -> None:
    dupes = seal_dupes(session, order_id, line_id, seal_ids)
    if dupes:
        raise ApiError(
            "VALIDATION_ERROR", f"seal id(s) already in use: {', '.join(dupes)}",
            details=[{"field": "seal_ids", "reason": s} for s in dupes], rule="R-057",
        )
