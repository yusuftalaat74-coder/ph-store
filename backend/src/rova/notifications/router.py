"""Notifications.

Every state machine in this codebase already names the events that deserve a
notification; what was missing was somewhere for them to land and a way for a
person to read them. `emit()` is that landing place, and it is deliberately
the only writer — a notification is created from a domain event, never typed
in by hand through an endpoint.

A notification is `QUEUED` when created and `SENT` when a channel adapter has
actually handed it off. There is no adapter for `SMS` or `WHATSAPP` in this
build, so those stay `QUEUED` rather than being marked `SENT` by an endpoint
that did not send anything — a queued notification is honest, a falsely sent
one is a bug you find in a dispute six months later.
"""
import json
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.core.clock import now
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.domain.enums import RoleCode

router = APIRouter(prefix="/v1", tags=["notifications"])

_ALL_ROLES = tuple(r for r in RoleCode)
_PLATFORM = (RoleCode.PLATFORM_ADMIN, RoleCode.OPS_REVIEWER, RoleCode.SUPPORT_AGENT)

Channel = Literal["PUSH", "SMS", "WHATSAPP", "IN_APP", "EMAIL", "PMS_WEBHOOK"]
Status = Literal["QUEUED", "SENT", "FAILED", "READ"]

# Channels this build can actually deliver on. `IN_APP` is delivered by the
# reader below; the rest need an adapter that does not exist yet.
DELIVERABLE_CHANNELS = ("IN_APP",)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def emit(session: Session, *, event_code: str, channel: str = "IN_APP",
         recipient_user_id: str | None = None, recipient_org_id: str | None = None,
         recipient_role: str | None = None, payload: dict | None = None,
         rendered_text: str | None = None) -> str:
    """The one writer. Domain code calls this; no endpoint does."""
    nid = new_id("ntf")
    session.execute(
        text("INSERT INTO notification (id, event_code, recipient_role, recipient_user_id, recipient_org_id, "
             "channel, payload, rendered_text, status) "
             "VALUES (:id, :ev, :rr, :ru, :ro, :ch, CAST(:p AS JSONB), :rt, 'QUEUED')"),
        {"id": nid, "ev": event_code, "rr": recipient_role, "ru": recipient_user_id,
         "ro": recipient_org_id, "ch": channel, "p": json.dumps(payload or {}), "rt": rendered_text},
    )
    return nid


@router.get("/notifications")
def list_notifications(status: Status | None = None, unread_only: bool = False,
                       limit: int = Query(default=50, ge=1, le=200),
                       principal: Principal = Depends(require_roles(*_ALL_ROLES)),
                       session: Session = Depends(get_session, scope="function")):
    """What this caller is addressed by: personally, as their organisation, or
    by their role. Nothing addressed to anyone else is visible here, whatever
    their role — a PlatformAdmin reads their own inbox on this route, not
    everybody's."""
    rows = session.execute(
        text("SELECT * FROM notification WHERE ("
             "  recipient_user_id = :u"
             "  OR (recipient_org_id IS NOT NULL AND recipient_org_id = :o)"
             "  OR (recipient_role IS NOT NULL AND recipient_role = ANY(:roles))"
             ") AND (CAST(:st AS TEXT) IS NULL OR status=:st) "
             "AND (:unread = false OR read_at IS NULL) "
             "ORDER BY created_at DESC LIMIT :lim"),
        {"u": principal.user_id, "o": principal.organisation_id,
         "roles": list(principal.roles) or [""], "st": status,
         "unread": unread_only, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/notifications/unread-count")
def unread_count(principal: Principal = Depends(require_roles(*_ALL_ROLES)),
                 session: Session = Depends(get_session, scope="function")):
    n = session.execute(
        text("SELECT count(*) FROM notification WHERE ("
             "  recipient_user_id = :u OR (recipient_org_id IS NOT NULL AND recipient_org_id = :o) "
             "  OR (recipient_role IS NOT NULL AND recipient_role = ANY(:roles))"
             ") AND read_at IS NULL"),
        {"u": principal.user_id, "o": principal.organisation_id,
         "roles": list(principal.roles) or [""]},
    ).scalar()
    return {"unread": n}


def _addressed_to(row: dict, principal: Principal) -> bool:
    return (row["recipient_user_id"] == principal.user_id
            or (row["recipient_org_id"] is not None and row["recipient_org_id"] == principal.organisation_id)
            or (row["recipient_role"] is not None and row["recipient_role"] in principal.roles))


def _notification_or_404(session: Session, notification_id: str, principal: Principal) -> dict:
    row = session.execute(text("SELECT * FROM notification WHERE id=:n"),
                          {"n": notification_id}).mappings().first()
    if row is None or not _addressed_to(dict(row), principal):
        raise ApiError("NOT_FOUND", "notification not found")
    return dict(row)


@router.get("/notifications/{notification_id}")
def get_notification(notification_id: str,
                     principal: Principal = Depends(require_roles(*_ALL_ROLES)),
                     session: Session = Depends(get_session, scope="function")):
    return _notification_or_404(session, notification_id, principal)


@router.post("/notifications/{notification_id}/read")
def mark_read(notification_id: str,
              principal: Principal = Depends(require_roles(*_ALL_ROLES)),
              session: Session = Depends(get_session, scope="function")):
    row = _notification_or_404(session, notification_id, principal)
    if row["read_at"] is not None:
        return row
    session.execute(
        text("UPDATE notification SET status='READ', read_at=:n, updated_at=:n WHERE id=:i AND read_at IS NULL"),
        {"n": now(), "i": notification_id},
    )
    return _notification_or_404(session, notification_id, principal)


@router.post("/notifications/read-all")
def mark_all_read(principal: Principal = Depends(require_roles(*_ALL_ROLES)),
                  session: Session = Depends(get_session, scope="function")):
    result = session.execute(
        text("UPDATE notification SET status='READ', read_at=:n, updated_at=:n WHERE read_at IS NULL AND ("
             "  recipient_user_id = :u OR (recipient_org_id IS NOT NULL AND recipient_org_id = :o) "
             "  OR (recipient_role IS NOT NULL AND recipient_role = ANY(:roles)))"),
        {"n": now(), "u": principal.user_id, "o": principal.organisation_id,
         "roles": list(principal.roles) or [""]},
    )
    return {"marked_read": result.rowcount}


@router.post("/notifications/dispatch")
def dispatch_queued(limit: int = Query(default=100, ge=1, le=500),
                    principal: Principal = Depends(require_roles(RoleCode.PLATFORM_ADMIN)),
                    session: Session = Depends(get_session, scope="function")):
    """Hands queued notifications to the channels this build can actually
    deliver on, and reports — rather than hides — the ones it cannot.

    A channel with no adapter is left `QUEUED`. It is not marked `SENT`, and
    it is not marked `FAILED` either: nothing was attempted, so neither word
    would be true."""
    rows = session.execute(
        text("SELECT id, channel FROM notification WHERE status='QUEUED' ORDER BY created_at ASC LIMIT :lim"),
        {"lim": limit},
    ).mappings().all()
    sent, undeliverable = 0, {}
    for row in rows:
        if row["channel"] in DELIVERABLE_CHANNELS:
            session.execute(
                text("UPDATE notification SET status='SENT', sent_at=:n, updated_at=:n "
                     "WHERE id=:i AND status='QUEUED'"),
                {"n": now(), "i": row["id"]},
            )
            sent += 1
        else:
            undeliverable[row["channel"]] = undeliverable.get(row["channel"], 0) + 1
    return {"sent": sent, "left_queued_no_adapter": undeliverable,
            "deliverable_channels": list(DELIVERABLE_CHANNELS)}


@router.get("/notifications-admin/queue")
def admin_queue(channel: Channel | None = None, status: Status | None = None,
                limit: int = Query(default=100, ge=1, le=500),
                principal: Principal = Depends(require_roles(*_PLATFORM)),
                session: Session = Depends(get_session, scope="function")):
    """The operations view of the whole queue, separate from a person's own
    inbox so the two can never be confused for each other."""
    rows = session.execute(
        text("SELECT * FROM notification WHERE (CAST(:ch AS TEXT) IS NULL OR channel=:ch) "
             "AND (CAST(:st AS TEXT) IS NULL OR status=:st) ORDER BY created_at DESC LIMIT :lim"),
        {"ch": channel, "st": status, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/notifications-admin/summary")
def admin_summary(principal: Principal = Depends(require_roles(*_PLATFORM)),
                  session: Session = Depends(get_session, scope="function")):
    rows = session.execute(
        text("SELECT channel, status, count(*) AS n FROM notification GROUP BY channel, status "
             "ORDER BY channel, status")
    ).mappings().all()
    return {"items": [dict(r) for r in rows]}
