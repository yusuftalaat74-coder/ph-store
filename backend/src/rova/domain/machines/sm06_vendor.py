"""SM-06 — vendor_account (A4.2 row SM-06).

Implemented subset: ONBOARDING -> ACTIVE (APPROVE, guarded by R-107/R-111/
R-112/R-143) and REJECT_VERIFICATION, plus MANUAL_SUSPEND / REINSTATE /
CLOSE, RESUBMIT. `LICENCE_WARNING` / `LICENCE_RENEWED` / `LICENCE_EXPIRED`
added by the SM-08/jobs session: without them `LICENCE_EXPIRING` was an
unreachable state (a genuine reachability bug against A4.1's own invariant —
nothing produced it), so `ACTIVE -> LICENCE_EXPIRING -> {ACTIVE, SUSPENDED}`
is wired here, fired by SM-08's `ENTER_WARNING_WINDOW`/`RENEW`/`EXPIRE`.
HEALTH_SUSPEND is still out of this session's reduced scope (no VendorScore
suspension job wired) but the trigger vocabulary and states below are
otherwise exactly A4.2's.

Item-2 fix (backend-review-r1.md): `LICENCE_EXPIRED` fired but performed none
of its A4.2-specified effects. Ranking exclusion needed no new code — A6.1's
`rova/ranking/inputs.py` already filters candidates to
`vendor_account.status IN ('ACTIVE','LICENCE_EXPIRING')`, so a `SUSPENDED`
vendor is structurally invisible to the ranking engine the moment this
transition lands; a test now asserts that directly rather than leaving it
implicit. Line lapse is new: every `LINE_FULL` order_line of an `ACCEPTED`
order of this vendor with no delivery_job beyond `CREATED` is walked through
SM-04 `VENDOR_LICENCE_LAPSE`, which reroutes it (excluding this vendor) via
the ordinary short-line machinery — never a raw order_line UPDATE."""
from sqlalchemy import text

from rova.domain.enums import RoleCode, VendorStatus
from rova.domain.fsm import Ctx, GuardResult, Machine, Transition

_AD_CF = frozenset({RoleCode.PLATFORM_ADMIN, RoleCode.COMPLIANCE_OFFICER})


def _guard_approve(ctx: Ctx) -> GuardResult:
    vendor_id = ctx.subject_id
    session = ctx.session
    alvara = session.execute(
        text("SELECT status FROM licence WHERE holder_type='VENDOR' AND holder_id=:v AND type='WHOLESALE_ALVARA' "
             "ORDER BY created_at DESC LIMIT 1"),
        {"v": vendor_id},
    ).scalar()
    if alvara != "VALID":
        return GuardResult.failed("wholesale alvará is not VALID", rule="R-111")
    attested = session.execute(
        text("SELECT sourcing_attestation FROM vendor_account WHERE id=:v"), {"v": vendor_id}
    ).scalar()
    if not attested:
        return GuardResult.failed("sourcing attestation (Art. 40) not given", rule="R-112")
    agreement_ok = session.execute(
        text("SELECT 1 FROM vendor_agreement WHERE vendor_id=:v AND status='ACTIVE' AND multi_vendor_clause_ack"),
        {"v": vendor_id},
    ).first()
    if not agreement_ok:
        return GuardResult.failed("no ACTIVE vendor_agreement with multi_vendor_clause_ack", rule="R-143")
    return GuardResult.passed()


def _effect_licence_expired(ctx: Ctx) -> None:
    """R-108/R-121: cascade the vendor's licence expiry onto every order
    still in flight but not yet dispatched (SM-04 VENDOR_LICENCE_LAPSE ->
    LINE_SHORT confirmed_qty=0, then either reroutes to another vendor or,
    if that empties the order, cancels it via NOTHING_TO_DISPATCH — all via
    the ordinary machinery, not a raw write). Orders already DISPATCHED are
    left alone (R-121: "already dispatched, continues with a compliance
    flag")."""
    from rova.domain.machines.registry import MACHINES
    session = ctx.session
    lines = session.execute(
        text(
            "SELECT ol.id FROM order_line ol JOIN \"order\" o ON o.id = ol.order_id "
            "WHERE o.vendor_id=:v AND o.status='ACCEPTED' AND ol.fulfilment_status='LINE_FULL' "
            "AND NOT EXISTS (SELECT 1 FROM delivery_job dj WHERE dj.order_id=o.id AND dj.status<>'CREATED')"
        ),
        {"v": ctx.subject_id},
    ).mappings().all()
    for line in lines:
        MACHINES["SM-04"].apply(session, line["id"], "VENDOR_LICENCE_LAPSE", "SYSTEM")


MACHINE = Machine(
    code="SM-06",
    subject_table="vendor_account",
    status_column="status",
    transitions=[
        Transition("SM-06", (VendorStatus.ONBOARDING,), VendorStatus.ACTIVE, "APPROVE",
                   frozenset({RoleCode.COMPLIANCE_OFFICER}), guard=_guard_approve,
                   rule_refs=("R-107", "R-111", "R-112", "R-143")),
        Transition("SM-06", (VendorStatus.ONBOARDING,), VendorStatus.REJECTED, "REJECT_VERIFICATION",
                   frozenset({RoleCode.COMPLIANCE_OFFICER})),
        Transition("SM-06", (VendorStatus.REJECTED,), VendorStatus.ONBOARDING, "RESUBMIT",
                   frozenset({RoleCode.VENDOR_ADMIN}), rule_refs=("R-127",)),
        Transition("SM-06", (VendorStatus.ACTIVE, VendorStatus.LICENCE_EXPIRING), VendorStatus.SUSPENDED,
                   "MANUAL_SUSPEND", _AD_CF, extra_set=lambda ctx: {"suspension_cause": "MANUAL_INCIDENT"}),
        Transition("SM-06", (VendorStatus.SUSPENDED,), VendorStatus.ACTIVE, "REINSTATE", _AD_CF,
                   extra_set=lambda ctx: {"suspension_cause": None}),
        Transition("SM-06", (VendorStatus.ACTIVE, VendorStatus.SUSPENDED, VendorStatus.LICENCE_EXPIRING),
                   VendorStatus.CLOSED, "CLOSE", _AD_CF),
        Transition("SM-06", (VendorStatus.ACTIVE,), VendorStatus.LICENCE_EXPIRING, "LICENCE_WARNING",
                   frozenset({"SYSTEM"})),
        Transition("SM-06", (VendorStatus.LICENCE_EXPIRING,), VendorStatus.ACTIVE, "LICENCE_RENEWED",
                   frozenset({"SYSTEM", RoleCode.COMPLIANCE_OFFICER})),
        Transition("SM-06", (VendorStatus.LICENCE_EXPIRING,), VendorStatus.SUSPENDED, "LICENCE_EXPIRED",
                   frozenset({"SYSTEM"}), effects=_effect_licence_expired,
                   extra_set=lambda ctx: {"suspension_cause": "LICENCE_EXPIRED"}),
    ],
)
