"""SM-24 — support_thread.active_handler (addendum §A11, A4.2 row SM-24,
R-177/R-178). Implemented subset: the full three-state cycle. `AssistantTurn`
outcome classification and the `CFG-SUPPORT-LOOP-BREAK-REFUSALS` counter
(R-177 conditions 1 and 3) are out of scope (A18, no assistant layer);
`ESCALATE` here fires on an explicit human request only (condition 2), which
is what the trigger vocabulary above exposes to a caller anyway."""
from sqlalchemy import text

from rova.core.clock import now
from rova.core.ids import new_id
from rova.domain import timers
from rova.domain.enums import ActiveHandler, RoleCode
from rova.domain.fsm import Ctx, GuardResult, Machine, Transition

_AGENT = frozenset({RoleCode.SUPPORT_AGENT, RoleCode.OPS_REVIEWER})

SUBJECT = "support_thread"
POLICY = "HUMAN_HANDOVER_RESPONSE"


def _effect_escalate(ctx: Ctx) -> None:
    session = ctx.session
    ticket_id = new_id("tkt")
    pharmacy = session.execute(
        text("SELECT organisation_id FROM pharmacy_account WHERE id=:p"), {"p": ctx.subject_row["pharmacy_id"]}
    ).mappings().one()
    session.execute(
        text(
            "INSERT INTO ticket (id, thread_id, subject_org_id, raised_by_user_id, status) VALUES "
            "(:id, :th, :org, :u, 'OPEN')"
        ),
        {"id": ticket_id, "th": ctx.subject_id, "org": pharmacy["organisation_id"], "u": ctx.actor_user_id},
    )
    session.execute(text("UPDATE support_thread SET active_ticket_id=:t WHERE id=:id"),
                     {"t": ticket_id, "id": ctx.subject_id})
    timers.start(session, policy_type=POLICY, subject_type=SUBJECT, subject_id=ctx.subject_id,
                 cfg_key="CFG-SUPPORT-HANDOVER-SLA-MINUTES", unit="MINUTES")


def _effect_human_takes_over(ctx: Ctx) -> None:
    session = ctx.session
    if ctx.subject_row["active_ticket_id"]:
        session.execute(text("UPDATE ticket SET status='IN_PROGRESS', updated_at=:n WHERE id=:id"),
                         {"n": now(), "id": ctx.subject_row["active_ticket_id"]})
    timers.cancel(session, policy_type=POLICY, subject_type=SUBJECT, subject_id=ctx.subject_id)


def _guard_ticket_closed(ctx: Ctx) -> GuardResult:
    ticket_id = ctx.subject_row["active_ticket_id"]
    if not ticket_id:
        return GuardResult.failed("no active ticket")
    status = ctx.session.execute(text("SELECT status FROM ticket WHERE id=:id"), {"id": ticket_id}).scalar()
    if status not in ("RESOLVED", "CLOSED"):
        return GuardResult.failed("ticket is not RESOLVED/CLOSED yet")
    return GuardResult.passed()


MACHINE = Machine(
    code="SM-24",
    subject_table=SUBJECT,
    status_column="active_handler",
    transitions=[
        Transition("SM-24", (ActiveHandler.BOT,), ActiveHandler.ESCALATING, "ESCALATE",
                   frozenset({"SYSTEM"} | {RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER}),
                   effects=_effect_escalate, rule_refs=("R-177",)),
        Transition("SM-24", (ActiveHandler.ESCALATING,), ActiveHandler.HUMAN, "HUMAN_TAKES_OVER", _AGENT,
                   effects=_effect_human_takes_over),
        Transition("SM-24", (ActiveHandler.HUMAN,), ActiveHandler.BOT, "TICKET_CLOSED", frozenset({"SYSTEM"}),
                   guard=_guard_ticket_closed, extra_set=lambda ctx: {"active_ticket_id": None},
                   rule_refs=("R-178",)),
    ],
)
