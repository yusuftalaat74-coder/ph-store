"""publish_pending(session) — called from `rova jobs tick` (JOBS["office_outbox"]).

In seq order, ONE message in flight per aggregate_id; backoff 30s, 1m, 2m,
5m, 15m, 30m, 1h, 2h, 4h, 8h then DEAD; a 4xx other than 401/408/429 is DEAD
at once (SPEC 5.4). integration_outbox is infrastructure, like sla_timer: its
status is written directly, it has no SM-xx machine."""
import json
from datetime import timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.core.clock import now
from rova.core.db import get_sessionmaker
from rova.integrations.office import config, signing

BACKOFF_SECONDS = [30, 60, 120, 300, 900, 1800, 3600, 7200, 14400, 28800]
OFFICE_EVENTS_PATH = "/office/v1/integration/phstore/events"

_client = None


def set_http_client(client) -> None:
    global _client
    _client = client


def http_client():
    global _client
    if _client is None:
        _client = httpx.Client(timeout=10.0)
    return _client


def envelope(m) -> dict:
    return {"event_id": f"evt_{m['id']}", "event_type": m["event_type"], "source": "phstore", "seq": m["seq"],
            "occurred_at": m["created_at"].isoformat(), "aggregate_type": m["aggregate_type"],
            "aggregate_id": m["aggregate_id"], "aggregate_version": m["aggregate_version"], "payload": m["payload"]}


def send(m) -> tuple[bool, bool, str | None]:
    env = envelope(m)
    body = json.dumps(env, separators=(",", ":"), sort_keys=True).encode()
    try:
        resp = http_client().post(config.url() + OFFICE_EVENTS_PATH, content=body,
                                  headers=signing.headers(config.outbound_secret(), body, env["event_id"]))
    except Exception as exc:
        return False, False, f"transport: {exc.__class__.__name__}: {exc}"[:500]
    if 200 <= resp.status_code < 300:
        return True, False, None
    return False, 400 <= resp.status_code < 500 and resp.status_code not in (401, 408, 429), \
        f"HTTP {resp.status_code}: {resp.text[:300]}"


def publish_pending(session: Session, limit: int = 200) -> int:
    if not config.emitting() or not config.url():
        return 0
    sent = 0
    for _ in range(20):
        batch = session.execute(
            text("SELECT m.* FROM integration_outbox m WHERE m.status IN ('PENDING','FAILED') AND m.next_attempt_at <= :n "
                 "AND NOT EXISTS (SELECT 1 FROM integration_outbox e WHERE e.aggregate_type=m.aggregate_type "
                 "AND e.aggregate_id=m.aggregate_id AND e.seq < m.seq AND e.status <> 'SENT') ORDER BY m.seq LIMIT :l"),
            {"n": now(), "l": limit},
        ).mappings().all()
        if not batch:
            break
        progressed, blocked = False, set()
        for m in batch:
            key = (m["aggregate_type"], m["aggregate_id"])
            if key in blocked:
                continue
            ok, dead, err = send(m)
            attempts = m["attempts"] + 1
            if ok:
                session.execute(text("UPDATE integration_outbox SET status='SENT', sent_at=:n, attempts=:a, last_error=NULL "
                                     "WHERE id=:id"), {"n": now(), "a": attempts, "id": m["id"]})
                sent += 1
                progressed = True
            else:
                blocked.add(key)
                if dead or attempts >= len(BACKOFF_SECONDS):
                    session.execute(text("UPDATE integration_outbox SET status='DEAD', attempts=:a, last_error=:e WHERE id=:id"),
                                    {"a": attempts, "e": err, "id": m["id"]})
                else:
                    session.execute(text("UPDATE integration_outbox SET status='FAILED', attempts=:a, last_error=:e, "
                                         "next_attempt_at=:nx WHERE id=:id"),
                                    {"a": attempts, "e": err, "id": m["id"],
                                     "nx": now() + timedelta(seconds=BACKOFF_SECONDS[attempts - 1])})
            session.commit()
        if not progressed:
            break
    return sent


def replay_from(session: Session, from_seq: int) -> dict:
    """Re-send from `from_seq` with the ORIGINAL event ids (Office dedups)."""
    stats = {"resent": 0, "failed": 0}
    for m in session.execute(text("SELECT * FROM integration_outbox WHERE seq >= :s AND status='SENT' ORDER BY seq"),
                             {"s": from_seq}).mappings().all():
        ok, _, _ = send(m)
        stats["resent" if ok else "failed"] += 1
    session.execute(text("UPDATE integration_outbox SET status='PENDING', attempts=0, next_attempt_at=:n "
                         "WHERE seq >= :s AND status='DEAD'"), {"n": now(), "s": from_seq})
    return stats


def run() -> int:
    """JOBS["office_outbox"]: publish, then retry FAILED inbound events."""
    if not config.emitting():
        return 0
    from rova.domain.hooks import wire
    wire()
    session = get_sessionmaker()()
    try:
        n = publish_pending(session)
        session.commit()
    finally:
        session.close()
    if config.enabled():
        from rova.integrations.office import inbound
        inbound.retry_failed()
    return n
