"""SM-07 — pharmacy_account. Implemented subset mirrors SM-06, including the
same `LICENCE_WARNING`/`LICENCE_RENEWED`/`LICENCE_EXPIRED` reachability fix
(fired by SM-08) described there."""
from rova.domain.enums import PharmacyStatus, RoleCode
from rova.domain.fsm import GuardResult, Machine, Transition

_AD_CF = frozenset({RoleCode.PLATFORM_ADMIN, RoleCode.COMPLIANCE_OFFICER})
# D-6 (signup spec): the same three reviewer roles SM-08 accepts, because
# SM-08 APPROVE forwards its own actor to this machine's APPROVE.
_REVIEWERS = frozenset({RoleCode.COMPLIANCE_OFFICER, RoleCode.PLATFORM_ADMIN, RoleCode.OPS_REVIEWER})


def _guard_approve(ctx) -> GuardResult:
    row = ctx.session.execute(
        __import__("sqlalchemy").text(
            "SELECT status FROM licence WHERE holder_type='PHARMACY' AND holder_id=:p "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        {"p": ctx.subject_id},
    ).scalar()
    if row not in ("VALID", "EXPIRING_SOON"):
        return GuardResult.failed("retail licence is not VALID", rule="R-113")
    return GuardResult.passed()


MACHINE = Machine(
    code="SM-07",
    subject_table="pharmacy_account",
    status_column="status",
    transitions=[
        Transition("SM-07", (PharmacyStatus.ONBOARDING,), PharmacyStatus.ACTIVE, "APPROVE",
                   _REVIEWERS, guard=_guard_approve, rule_refs=("R-113",)),
        Transition("SM-07", (PharmacyStatus.ONBOARDING,), PharmacyStatus.REJECTED, "REJECT_VERIFICATION",
                   _REVIEWERS),
        Transition("SM-07", (PharmacyStatus.REJECTED,), PharmacyStatus.ONBOARDING, "RESUBMIT",
                   frozenset({RoleCode.PHARMACY_ADMIN})),
        Transition("SM-07", (PharmacyStatus.ACTIVE, PharmacyStatus.LICENCE_EXPIRING), PharmacyStatus.SUSPENDED,
                   "MANUAL_SUSPEND", _AD_CF, extra_set=lambda ctx: {"suspension_cause": "MANUAL_INCIDENT"}),
        Transition("SM-07", (PharmacyStatus.SUSPENDED,), PharmacyStatus.ACTIVE, "REINSTATE", _AD_CF,
                   extra_set=lambda ctx: {"suspension_cause": None}),
        Transition("SM-07", (PharmacyStatus.ACTIVE, PharmacyStatus.SUSPENDED, PharmacyStatus.LICENCE_EXPIRING),
                   PharmacyStatus.CLOSED, "CLOSE", _AD_CF),
        Transition("SM-07", (PharmacyStatus.ACTIVE,), PharmacyStatus.LICENCE_EXPIRING, "LICENCE_WARNING",
                   frozenset({"SYSTEM"})),
        Transition("SM-07", (PharmacyStatus.LICENCE_EXPIRING,), PharmacyStatus.ACTIVE, "LICENCE_RENEWED",
                   frozenset({"SYSTEM", RoleCode.COMPLIANCE_OFFICER})),
        Transition("SM-07", (PharmacyStatus.LICENCE_EXPIRING,), PharmacyStatus.SUSPENDED, "LICENCE_EXPIRED",
                   frozenset({"SYSTEM"}), extra_set=lambda ctx: {"suspension_cause": "LICENCE_EXPIRED"}),
    ],
)
