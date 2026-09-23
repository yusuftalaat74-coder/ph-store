"""SM-23 — pms_link (addendum §A10, A4.2 row SM-23, R-167-R-169)."""
from sqlalchemy import text

from rova.core.clock import now
from rova.core.ids import new_id
from rova.domain.enums import PmsLinkStatus, RoleCode
from rova.domain.fsm import Ctx, GuardResult, Machine, Transition

_PHARMACY_ADMIN = frozenset({RoleCode.PHARMACY_ADMIN})

SUBJECT = "pms_link"


def _guard_accept_terms(ctx: Ctx) -> GuardResult:
    status = ctx.session.execute(
        text("SELECT status FROM pharmacy_account WHERE id=:p"), {"p": ctx.subject_row["pharmacy_id"]}
    ).scalar()
    if status != "ACTIVE":
        return GuardResult.failed("pharmacy must be ACTIVE to link a PMS")
    terms_version_id = ctx.kwargs.get("terms_version_id")
    if not terms_version_id:
        return GuardResult.failed("terms_version_id is required")
    terms = ctx.session.execute(
        text("SELECT code, mentions_pms_aggregation FROM terms_version WHERE id=:t"), {"t": terms_version_id}
    ).mappings().first()
    if terms is None or terms["code"] != "PMS_LINK_TERMS" or not terms["mentions_pms_aggregation"]:
        return GuardResult.failed("terms_version must be PMS_LINK_TERMS with mentions_pms_aggregation=true",
                                   rule="R-161")
    return GuardResult.passed()


def _effect_accept_terms(ctx: Ctx) -> None:
    session = ctx.session
    consent_id = new_id("cns")
    session.execute(
        text(
            "INSERT INTO consent_record (id, subject_type, subject_id, purpose, terms_version_id, "
            "granted_by_user_id) VALUES (:id, 'PHARMACY_ACCOUNT', :p, 'PMS_LINK', :t, :u)"
        ),
        {"id": consent_id, "p": ctx.subject_row["pharmacy_id"], "t": ctx.kwargs.get("terms_version_id"),
         "u": ctx.actor_user_id},
    )
    session.execute(text("UPDATE pms_link SET consent_record_id=:c WHERE id=:id"),
                     {"c": consent_id, "id": ctx.subject_id})


def _effect_revoke(ctx: Ctx) -> None:
    ctx.session.execute(
        text(
            "INSERT INTO audit_event (id, actor_role, actor_user_id, action_code, subject_type, subject_id) "
            "VALUES (:id, :role, :uid, 'CONSENT_WITHDRAWN', :st, :sid)"
        ),
        {"id": new_id("aud"), "role": ctx.actor_role, "uid": ctx.actor_user_id, "st": SUBJECT, "sid": ctx.subject_id},
    )


MACHINE = Machine(
    code="SM-23",
    subject_table=SUBJECT,
    status_column="status",
    transitions=[
        Transition("SM-23", (PmsLinkStatus.PENDING_CONSENT,), PmsLinkStatus.LINKED, "ACCEPT_TERMS",
                   _PHARMACY_ADMIN, guard=_guard_accept_terms, effects=_effect_accept_terms,
                   extra_set=lambda ctx: {"linked_at": now()}),
        Transition("SM-23", (PmsLinkStatus.PENDING_CONSENT,), PmsLinkStatus.REVOKED, "DECLINE", _PHARMACY_ADMIN),
        Transition("SM-23", (PmsLinkStatus.LINKED,), PmsLinkStatus.SUSPENDED, "SUSPEND", frozenset({"SYSTEM"})),
        Transition("SM-23", (PmsLinkStatus.SUSPENDED,), PmsLinkStatus.LINKED, "RESUME", frozenset({"SYSTEM"})),
        Transition("SM-23", (PmsLinkStatus.LINKED, PmsLinkStatus.SUSPENDED), PmsLinkStatus.REVOKED, "REVOKE",
                   _PHARMACY_ADMIN | {"SYSTEM"}, effects=_effect_revoke,
                   extra_set=lambda ctx: {"revoked_at": now()}),
    ],
)
