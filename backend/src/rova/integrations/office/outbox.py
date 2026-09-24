"""Store -> Office outbox (SPEC 5.4). `enqueue` runs inside the transaction of
the business event, so the event exists iff the fact committed. A no-op
unless the link is enabled (or emitting in shadow mode)."""
import json
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.core.ids import new_id
from rova.integrations.office import config

# set while an event FROM PH Office is being applied: transitions it causes are
# not echoed back to Office as new events
applying_office_event: ContextVar[str | None] = ContextVar("applying_office_event", default=None)


def _default(v):
    return str(v)


def state_version(session: Session, subject_id: str) -> int:
    """SPEC 5.2: aggregate_version = number of state transitions of the subject so far."""
    return session.execute(text("SELECT COUNT(*) FROM state_transition WHERE subject_id=:s"), {"s": subject_id}).scalar()


def enqueue(session: Session, event_type: str, aggregate_type: str, aggregate_id: str, payload: dict,
            aggregate_version: int | None = None) -> str | None:
    if not config.emitting() or applying_office_event.get():
        return None
    if aggregate_version is None:
        aggregate_version = state_version(session, aggregate_id)
    oid = new_id("obx")
    session.execute(
        text("INSERT INTO integration_outbox (id, event_type, aggregate_type, aggregate_id, aggregate_version, payload) "
             "VALUES (:id, :t, :at, :aid, :v, CAST(:p AS JSONB))"),
        {"id": oid, "t": event_type, "at": aggregate_type, "aid": aggregate_id, "v": aggregate_version,
         "p": json.dumps(payload, default=_default)},
    )
    return oid
