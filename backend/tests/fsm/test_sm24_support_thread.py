"""A17 'Every state machine' row for SM-24 (addendum §A11, R-177/R-178) —
support_thread.active_handler: ESCALATE (creates a Ticket, starts SlaTimer
(HUMAN_HANDOVER_RESPONSE)), HUMAN_TAKES_OVER (cancels the timer), TICKET_CLOSED
(guarded on the ticket actually being RESOLVED/CLOSED)."""
import pytest
from sqlalchemy import text

from rova.core.errors import ApiError
from rova.domain.fsm import Principal
from rova.domain.machines.registry import MACHINES

MACHINE = MACHINES["SM-24"]

PHARMACY_ADMIN = Principal(user_id=None, roles=frozenset({"PharmacyAdmin"}))
AGENT = Principal(user_id=None, roles=frozenset({"SupportAgent"}))


@pytest.fixture
def thread(session):
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "('org_sm24', '924', 'P24', 'p24', 'PHARMACY')"
    ))
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES ('pha_sm24', 'org_sm24', 'MAPUTO_CIDADE', 'A', 'P24', 'X', 0, 0)"
    ))
    session.execute(text(
        "INSERT INTO support_thread (id, pharmacy_id, active_handler) VALUES ('sth_sm24', 'pha_sm24', 'BOT')"
    ))
    yield "sth_sm24"
    session.rollback()


def test_illegal_transition_raises(session, thread):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, thread, "HUMAN_TAKES_OVER", AGENT)  # can't take over before ESCALATE
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_wrong_actor_forbidden(session, thread):
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, thread, "ESCALATE", AGENT)  # SupportAgent isn't in SM-24's ESCALATE actor set
    assert exc.value.code == "FORBIDDEN"


def test_escalate_creates_ticket_and_starts_timer(session, thread):
    row = MACHINE.apply(session, thread, "ESCALATE", PHARMACY_ADMIN)
    assert row["active_handler"] == "ESCALATING"
    assert row["active_ticket_id"] is not None
    ticket = session.execute(
        text("SELECT status FROM ticket WHERE id=:t"), {"t": row["active_ticket_id"]}
    ).mappings().one()
    assert ticket["status"] == "OPEN"
    timer = session.execute(
        text("SELECT status FROM sla_timer WHERE policy_type='HUMAN_HANDOVER_RESPONSE' AND subject_id=:s"),
        {"s": thread},
    ).mappings().one()
    assert timer["status"] == "RUNNING"


def test_human_takes_over_cancels_timer(session, thread):
    MACHINE.apply(session, thread, "ESCALATE", PHARMACY_ADMIN)
    row = MACHINE.apply(session, thread, "HUMAN_TAKES_OVER", AGENT)
    assert row["active_handler"] == "HUMAN"
    ticket = session.execute(
        text("SELECT status FROM ticket WHERE id=:t"), {"t": row["active_ticket_id"]}
    ).mappings().one()
    assert ticket["status"] == "IN_PROGRESS"
    timer = session.execute(
        text("SELECT status FROM sla_timer WHERE policy_type='HUMAN_HANDOVER_RESPONSE' AND subject_id=:s"),
        {"s": thread},
    ).mappings().one()
    assert timer["status"] == "CANCELLED"


def test_ticket_closed_guard_and_transition(session, thread):
    MACHINE.apply(session, thread, "ESCALATE", PHARMACY_ADMIN)
    row = MACHINE.apply(session, thread, "HUMAN_TAKES_OVER", AGENT)
    ticket_id = row["active_ticket_id"]
    with pytest.raises(ApiError) as exc:
        MACHINE.apply(session, thread, "TICKET_CLOSED", "SYSTEM")  # ticket still IN_PROGRESS
    assert exc.value.code == "GUARD_FAILED"

    session.execute(text("UPDATE ticket SET status='RESOLVED' WHERE id=:t"), {"t": ticket_id})
    row = MACHINE.apply(session, thread, "TICKET_CLOSED", "SYSTEM")
    assert row["active_handler"] == "BOT"
    assert row["active_ticket_id"] is None
