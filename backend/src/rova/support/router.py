"""Support — one thread per pharmacy, read like a chat.

This is the surface the product brief describes as "WhatsApp between us and
the customer only": the pharmacy sees a single continuous conversation, and
who is answering on the platform's side — a bot, or a human — is a property
of the thread (SM-24's `active_handler`), not a second inbox the pharmacy has
to find.

The vendor is never in this thread. A question that genuinely needs the
vendor is relayed by an agent through the ticket (`requires_vendor_relay`),
so the pharmacy keeps one conversation and never ends up negotiating with a
supplier in a channel nobody is auditing.
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
from rova.domain.machines.registry import MACHINES

router = APIRouter(prefix="/v1/support", tags=["support"])

_PHARMACY = (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER, RoleCode.PHARMACY_RECEIVER)
_AGENT = (RoleCode.SUPPORT_AGENT, RoleCode.OPS_REVIEWER)
_AGENT_OR_ADMIN = _AGENT + (RoleCode.PLATFORM_ADMIN,)

MessageChannel = Literal["APP", "WHATSAPP_TEXT", "PHONE_SUMMARY"]
TicketStatus = Literal["OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _thread_or_404(session: Session, thread_id: str, principal: Principal) -> dict:
    row = session.execute(text("SELECT * FROM support_thread WHERE id=:t"),
                          {"t": thread_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "thread not found")
    if principal.pharmacy_id and row["pharmacy_id"] != principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "thread not found")
    if principal.vendor_id:
        raise ApiError("FORBIDDEN", "support threads are between the pharmacy and the platform only")
    return dict(row)


def _ensure_thread(session: Session, pharmacy_id: str) -> str:
    """One thread per pharmacy, forever. A pharmacy that writes after six
    months of silence continues the same conversation rather than opening a
    second one — which is the whole reason this reads like a chat."""
    existing = session.execute(text("SELECT id FROM support_thread WHERE pharmacy_id=:p"),
                               {"p": pharmacy_id}).scalar()
    if existing:
        return existing
    tid = new_id("thr")
    session.execute(
        text("INSERT INTO support_thread (id, pharmacy_id, active_handler, last_activity_at) "
             "VALUES (:id, :p, 'BOT', :t)"),
        {"id": tid, "p": pharmacy_id, "t": now()},
    )
    return tid


@router.get("/threads/me")
def my_thread(principal: Principal = Depends(require_roles(*_PHARMACY)),
              session: Session = Depends(get_session, scope="function")):
    """The pharmacy's own thread, created on first use."""
    if not principal.pharmacy_id:
        raise ApiError("FORBIDDEN", "principal is not a pharmacy member")
    tid = _ensure_thread(session, principal.pharmacy_id)
    return _thread_view(session, tid)


def _thread_view(session: Session, thread_id: str, limit: int = 100) -> dict:
    row = session.execute(text("SELECT * FROM support_thread WHERE id=:t"),
                          {"t": thread_id}).mappings().one()
    messages = session.execute(
        text("SELECT * FROM thread_message WHERE thread_id=:t ORDER BY created_at ASC LIMIT :lim"),
        {"t": thread_id, "lim": limit},
    ).mappings().all()
    ticket = None
    if row["active_ticket_id"]:
        ticket = dict(session.execute(text("SELECT * FROM ticket WHERE id=:i"),
                                      {"i": row["active_ticket_id"]}).mappings().one())
    return {**dict(row), "messages": [dict(m) for m in messages], "active_ticket": ticket}


@router.get("/threads")
def list_threads(active_handler: Literal["BOT", "ESCALATING", "HUMAN"] | None = None,
                 limit: int = Query(default=50, ge=1, le=200),
                 principal: Principal = Depends(require_roles(*_AGENT_OR_ADMIN)),
                 session: Session = Depends(get_session, scope="function")):
    """The agent's inbox. `ESCALATING` first — those are the pharmacies
    waiting for a human right now."""
    rows = session.execute(
        text("SELECT st.*, p.trade_name, "
             "(SELECT count(*) FROM thread_message m WHERE m.thread_id = st.id) AS message_count "
             "FROM support_thread st JOIN pharmacy_account p ON p.id = st.pharmacy_id "
             "WHERE (CAST(:ah AS TEXT) IS NULL OR st.active_handler=:ah) "
             "ORDER BY CASE st.active_handler WHEN 'ESCALATING' THEN 0 WHEN 'HUMAN' THEN 1 ELSE 2 END, "
             "st.last_activity_at DESC LIMIT :lim"),
        {"ah": active_handler, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/threads/{thread_id}")
def get_thread(thread_id: str, limit: int = Query(default=100, ge=1, le=500),
               principal: Principal = Depends(require_roles(*(_PHARMACY + _AGENT_OR_ADMIN))),
               session: Session = Depends(get_session, scope="function")):
    _thread_or_404(session, thread_id, principal)
    return _thread_view(session, thread_id, limit)


class PostMessage(Body):
    body: str = Field(min_length=1, max_length=4000)
    channel: MessageChannel = "APP"


@router.post("/threads/me/messages", status_code=201)
def pharmacy_message(body: PostMessage,
                     principal: Principal = Depends(require_roles(*_PHARMACY)),
                     session: Session = Depends(get_session, scope="function")):
    if not principal.pharmacy_id:
        raise ApiError("FORBIDDEN", "principal is not a pharmacy member")
    tid = _ensure_thread(session, principal.pharmacy_id)
    return _post(session, tid, sender_type="PHARMACY", user_id=principal.user_id,
                 channel=body.channel, body=body.body)


@router.post("/threads/{thread_id}/messages", status_code=201)
def agent_message(thread_id: str, body: PostMessage,
                  principal: Principal = Depends(require_roles(*_AGENT_OR_ADMIN)),
                  session: Session = Depends(get_session, scope="function")):
    thread = _thread_or_404(session, thread_id, principal)
    if thread["active_handler"] == "BOT":
        raise ApiError("GUARD_FAILED",
                       "take the thread over first: an agent reply while the handler is BOT would "
                       "leave the pharmacy unsure who it is talking to")
    return _post(session, thread_id, sender_type="AGENT", user_id=principal.user_id,
                 channel=body.channel, body=body.body, ticket_id=thread["active_ticket_id"])


def _post(session: Session, thread_id: str, *, sender_type: str, user_id: str | None,
          channel: str, body: str, ticket_id: str | None = None) -> dict:
    mid = new_id("msg")
    session.execute(
        text("INSERT INTO thread_message (id, thread_id, sender_type, sender_user_id, channel, body, ticket_id) "
             "VALUES (:id, :t, :st, :u, :ch, :b, :tk)"),
        {"id": mid, "t": thread_id, "st": sender_type, "u": user_id, "ch": channel,
         "b": body, "tk": ticket_id},
    )
    session.execute(text("UPDATE support_thread SET last_activity_at=:n, updated_at=:n WHERE id=:t"),
                    {"n": now(), "t": thread_id})
    return dict(session.execute(text("SELECT * FROM thread_message WHERE id=:id"),
                                {"id": mid}).mappings().one())


@router.post("/threads/me/escalate")
def escalate(principal: Principal = Depends(require_roles(RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER)),
             session: Session = Depends(get_session, scope="function")):
    """"I want a human." SM-24 opens the ticket and starts the handover SLA
    timer; nothing here does either by hand."""
    if not principal.pharmacy_id:
        raise ApiError("FORBIDDEN", "principal is not a pharmacy member")
    tid = _ensure_thread(session, principal.pharmacy_id)
    MACHINES["SM-24"].apply(session, tid, "ESCALATE", principal)
    return _thread_view(session, tid)


@router.post("/threads/{thread_id}/take-over")
def take_over(thread_id: str,
              principal: Principal = Depends(require_roles(*_AGENT)),
              session: Session = Depends(get_session, scope="function")):
    thread = _thread_or_404(session, thread_id, principal)
    MACHINES["SM-24"].apply(session, thread_id, "HUMAN_TAKES_OVER", principal)
    if thread["active_ticket_id"]:
        session.execute(text("UPDATE ticket SET opened_by_agent_user_id=:u, updated_at=:n WHERE id=:i"),
                        {"u": principal.user_id, "n": now(), "i": thread["active_ticket_id"]})
    return _thread_view(session, thread_id)


# ------------------------------------------------------------------ tickets

@router.get("/tickets")
def list_tickets(status: TicketStatus | None = None, limit: int = Query(default=50, ge=1, le=200),
                 principal: Principal = Depends(require_roles(*(_PHARMACY + _AGENT_OR_ADMIN))),
                 session: Session = Depends(get_session, scope="function")):
    org_filter = None
    if principal.pharmacy_id:
        org_filter = principal.organisation_id
    rows = session.execute(
        text("SELECT * FROM ticket WHERE (CAST(:o AS TEXT) IS NULL OR subject_org_id=:o) "
             "AND (CAST(:st AS TEXT) IS NULL OR status=:st) ORDER BY created_at DESC LIMIT :lim"),
        {"o": org_filter, "st": status, "lim": limit},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str,
               principal: Principal = Depends(require_roles(*(_PHARMACY + _AGENT_OR_ADMIN))),
               session: Session = Depends(get_session, scope="function")):
    row = session.execute(text("SELECT * FROM ticket WHERE id=:i"), {"i": ticket_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "ticket not found")
    if principal.pharmacy_id and row["subject_org_id"] != principal.organisation_id:
        raise ApiError("NOT_FOUND", "ticket not found")
    messages = session.execute(
        text("SELECT * FROM thread_message WHERE ticket_id=:i ORDER BY created_at ASC"), {"i": ticket_id},
    ).mappings().all()
    return {**dict(row), "messages": [dict(m) for m in messages]}


class PatchTicket(Body):
    related_order_id: str | None = None
    requires_vendor_relay: bool | None = None


@router.patch("/tickets/{ticket_id}")
def patch_ticket(ticket_id: str, body: PatchTicket,
                 principal: Principal = Depends(require_roles(*_AGENT_OR_ADMIN)),
                 session: Session = Depends(get_session, scope="function")):
    row = session.execute(text("SELECT * FROM ticket WHERE id=:i"), {"i": ticket_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "ticket not found")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise ApiError("VALIDATION_ERROR", "no fields to update")
    if fields.get("related_order_id"):
        if not session.execute(text('SELECT 1 FROM "order" WHERE id=:o'),
                               {"o": fields["related_order_id"]}).first():
            raise ApiError("NOT_FOUND", "order not found")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    session.execute(text(f"UPDATE ticket SET {sets}, updated_at = :n WHERE id = :i"),
                    {**fields, "n": now(), "i": ticket_id})
    return dict(session.execute(text("SELECT * FROM ticket WHERE id=:i"), {"i": ticket_id}).mappings().one())


class ResolveTicket(Body):
    resolution_note: str = Field(min_length=1, max_length=4000)


@router.post("/tickets/{ticket_id}/resolve")
def resolve_ticket(ticket_id: str, body: ResolveTicket,
                   principal: Principal = Depends(require_roles(*_AGENT_OR_ADMIN)),
                   session: Session = Depends(get_session, scope="function")):
    """Resolving the ticket writes the closing note into the thread — the
    pharmacy reads the answer in the same conversation, never in a separate
    "your ticket was closed" notice with no content — and then hands the
    thread back to the bot via SM-24 `TICKET_CLOSED`."""
    row = session.execute(text("SELECT * FROM ticket WHERE id=:i"), {"i": ticket_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "ticket not found")
    if row["status"] in ("RESOLVED", "CLOSED"):
        raise ApiError("ILLEGAL_TRANSITION", "ticket is already resolved")

    _post(session, row["thread_id"], sender_type="AGENT", user_id=principal.user_id,
          channel="APP", body=body.resolution_note, ticket_id=ticket_id)
    session.execute(
        text("UPDATE ticket SET status='RESOLVED', resolved_at=:n, updated_at=:n WHERE id=:i AND status <> 'RESOLVED'"),
        {"n": now(), "i": ticket_id},
    )
    thread = session.execute(text("SELECT * FROM support_thread WHERE id=:t"),
                             {"t": row["thread_id"]}).mappings().one()
    if thread["active_handler"] == "HUMAN":
        MACHINES["SM-24"].apply(session, row["thread_id"], "TICKET_CLOSED", "SYSTEM")
    return _thread_view(session, row["thread_id"])


@router.post("/tickets/{ticket_id}/close")
def close_ticket(ticket_id: str,
                 principal: Principal = Depends(require_roles(*_AGENT_OR_ADMIN)),
                 session: Session = Depends(get_session, scope="function")):
    row = session.execute(text("SELECT * FROM ticket WHERE id=:i"), {"i": ticket_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "ticket not found")
    if row["status"] == "CLOSED":
        raise ApiError("ILLEGAL_TRANSITION", "ticket is already closed")
    session.execute(text("UPDATE ticket SET status='CLOSED', updated_at=:n WHERE id=:i"),
                    {"n": now(), "i": ticket_id})
    return dict(session.execute(text("SELECT * FROM ticket WHERE id=:i"), {"i": ticket_id}).mappings().one())
