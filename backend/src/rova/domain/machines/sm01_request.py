"""SM-01 — request (§6 SM-01, A4.2 row SM-01).

Item 6 (backend-review-r1.md): the full 13-transition table is wired here
now, not the 4-transition CATALOGUE-only subset this module used to carry.
`AWAITING_ADMIN_APPROVAL` (R-122's admin-approval gate for a catalogue cart
over `PharmacyAccount.buyer_approval_threshold`) and `NORMALIZING` /
`AWAITING_CONFIRMATION` (the RFQ/ASSISTED-channel normalisation echo, before
any checkout step exists to drive it) are now reachable and have a real
exit, satisfying `tests/fsm/test_reachability_invariant.py` without the
named exemption that used to carry them. This is a machine-definition-level
fix: it makes every (state, trigger) pair in §6's own table a real,
independently guarded `Transition`, each individually testable — it does
not add the RFQ/ASSISTED ingestion or checkout HTTP endpoints that would
actually *drive* NORMALIZING/AWAITING_ADMIN_APPROVAL end-to-end in
production (out of this session's scope; see the executor manifest), the
same way SM-02's already-wired ACCEPT_LINES trigger doesn't imply RFQ
ingestion exists either.

`AssistedPharmacy` (§6's actor for the two `AWAITING_CONFIRMATION` exits) is
not a `membership.role_codes` value in this backend (§2.4/§3: an assisted
pharmacy has no direct login at all); مواصفة_المنتج_v1.0.md line 1790 is
explicit that every action `AssistedPharmacy` would take is instead taken
*for* it by `OpsReviewer`, acting on a channel-recorded confirmation
(R-115) — so those two transitions' actor set is `{PharmacyBuyer,
OpsReviewer}`, not a fabricated `AssistedPharmacy` role."""
from datetime import date
from decimal import Decimal

from sqlalchemy import text

from rova.core.clock import now
from rova.domain import timers
from rova.domain.enums import QuotationStatus, RequestStatus, RoleCode
from rova.domain.fsm import Ctx, GuardResult, Machine, Transition

_PHARMACY = frozenset({RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER, RoleCode.OPS_REVIEWER})
_BUYER = frozenset({RoleCode.PHARMACY_BUYER, RoleCode.PHARMACY_ADMIN})
_ADMIN = frozenset({RoleCode.PHARMACY_ADMIN})
# AssistedPharmacy acts through OpsReviewer (see module docstring, R-115)
_ASSISTED_OR_BUYER = frozenset({RoleCode.PHARMACY_BUYER, RoleCode.OPS_REVIEWER})
_SYSTEM = frozenset({"SYSTEM"})
POLICY_ADMIN_APPROVAL = "ADMIN_APPROVAL"
POLICY_CONFIRMATION = "CONFIRMATION"
SUBJECT = "request"


def _cart_total(ctx: Ctx) -> Decimal:
    """R-122: the catalogue cart's total, priced the same way checkout.py
    prices it (regulated -> price_reference, else the top ranked offer) —
    duplicated rather than imported from rova/ordering/checkout.py because
    that module owns SM-03 order creation (another executor's parallel
    scope this session; see the executor manifest) and a guard must not
    reach into it."""
    from rova.ranking.engine import rank
    from rova.ranking.inputs import build_ranking_input

    lines = ctx.session.execute(
        text("SELECT * FROM request_line WHERE request_id=:r"), {"r": ctx.subject_id}
    ).mappings().all()
    total = Decimal("0")
    for line in lines:
        product = ctx.session.execute(
            text("SELECT regulated_price FROM index_product WHERE id=:p"), {"p": line["index_product_id"]}
        ).mappings().one()
        if product["regulated_price"]:
            ref = ctx.session.execute(
                text(
                    "SELECT wholesale_derived_price FROM price_reference WHERE index_product_id=:p "
                    "AND effective_from <= :today ORDER BY effective_from DESC LIMIT 1"
                ),
                {"p": line["index_product_id"], "today": date.today()},
            ).mappings().first()
            if ref is None:
                continue
            total += ref["wholesale_derived_price"] * line["qty_requested"]
        else:
            result = rank(build_ranking_input(
                ctx.session, index_product_id=line["index_product_id"], qty_requested=line["qty_requested"],
                pharmacy_id=ctx.subject_row["pharmacy_id"], strategy=ctx.subject_row["allocation_strategy"],
            ))
            if result.ordered:
                total += result.ordered[0].price * line["qty_requested"]
    return total


def _cart_non_empty(ctx: Ctx) -> bool:
    return ctx.session.execute(
        text("SELECT 1 FROM request_line WHERE request_id=:r LIMIT 1"), {"r": ctx.subject_id}
    ).first() is not None


def _threshold(ctx: Ctx) -> Decimal | None:
    return ctx.session.execute(
        text("SELECT buyer_approval_threshold FROM pharmacy_account WHERE id=:p"),
        {"p": ctx.subject_row["pharmacy_id"]},
    ).scalar()


def _guard_submit_over_threshold(ctx: Ctx) -> GuardResult:
    if not _cart_non_empty(ctx):
        return GuardResult.failed("cart is empty", rule="R-030")
    threshold = _threshold(ctx)
    if threshold is None:
        return GuardResult.failed("no buyer_approval_threshold set; SUBMIT_CART applies instead", rule="R-122")
    if _cart_total(ctx) <= threshold:
        return GuardResult.failed("cart total does not exceed buyer_approval_threshold", rule="R-122")
    return GuardResult.passed()


def _guard_submit_within_threshold(ctx: Ctx) -> GuardResult:
    if not _cart_non_empty(ctx):
        return GuardResult.failed("cart is empty", rule="R-030")
    threshold = _threshold(ctx)
    if threshold is not None and _cart_total(ctx) > threshold:
        return GuardResult.failed(
            "cart total exceeds buyer_approval_threshold; requires PharmacyAdmin approval", rule="R-122",
        )
    return GuardResult.passed()


def _guard_admin_approve(ctx: Ctx) -> GuardResult:
    if not timers.is_running(ctx.session, policy_type=POLICY_ADMIN_APPROVAL, subject_type=SUBJECT,
                              subject_id=ctx.subject_id):
        return GuardResult.failed("SlaTimer(admin_approval) is not running (already expired)", rule="A-142")
    return GuardResult.passed()


def _effect_start_admin_approval_timer(ctx: Ctx) -> None:
    timers.start(ctx.session, policy_type=POLICY_ADMIN_APPROVAL, subject_type=SUBJECT, subject_id=ctx.subject_id,
                 cfg_key="CFG-ADMIN-APPROVAL-TIMEOUT-HOURS")


def _effect_cancel_admin_approval_timer(ctx: Ctx) -> None:
    timers.cancel(ctx.session, policy_type=POLICY_ADMIN_APPROVAL, subject_type=SUBJECT, subject_id=ctx.subject_id)


def _effect_start_confirmation_timer(ctx: Ctx) -> None:
    timers.start(ctx.session, policy_type=POLICY_CONFIRMATION, subject_type=SUBJECT, subject_id=ctx.subject_id,
                 cfg_key="CFG-CONFIRMATION-TIMEOUT-HOURS")


def _effect_cancel_confirmation_timer(ctx: Ctx) -> None:
    timers.cancel(ctx.session, policy_type=POLICY_CONFIRMATION, subject_type=SUBJECT, subject_id=ctx.subject_id)


def _guard_confirm(ctx: Ctx) -> GuardResult:
    if not timers.is_running(ctx.session, policy_type=POLICY_CONFIRMATION, subject_type=SUBJECT,
                              subject_id=ctx.subject_id):
        return GuardResult.failed("SlaTimer(confirmation) is not running (already expired)", rule="A-11")
    return GuardResult.passed()


def _guard_no_allocations_yet(ctx: Ctx) -> GuardResult:
    """CONFIRMED -> CANCELLED (RFQ/ASSISTED, all quotations terminal, zero
    Allocation created): the completion criterion §6 SM-01 adds so an RFQ
    request whose every Quotation ends EXPIRED/DECLINED doesn't stay
    CONFIRMED forever with no Order ever created to close it via
    ALL_ORDERS_TERMINAL."""
    quotations = ctx.session.execute(
        text("SELECT status FROM quotation WHERE request_id=:r"), {"r": ctx.subject_id}
    ).mappings().all()
    if not quotations:
        return GuardResult.failed("no Quotation exists for this request")
    terminal = {QuotationStatus.ACCEPTED, QuotationStatus.DECLINED, QuotationStatus.EXPIRED}
    if not all(q["status"] in terminal for q in quotations):
        return GuardResult.failed("not every Quotation has reached a terminal state")
    has_allocation = ctx.session.execute(
        text(
            "SELECT 1 FROM allocation a JOIN request_line rl ON rl.id = a.request_line_id "
            "WHERE rl.request_id = :r LIMIT 1"
        ),
        {"r": ctx.subject_id},
    ).first() is not None
    if has_allocation:
        return GuardResult.failed("at least one Allocation already exists")
    return GuardResult.passed()


def _guard_cancel_before_accepted(ctx: Ctx) -> GuardResult:
    """R-045: a pharmacy may cancel a confirmed multi-vendor request only
    while no child Order has reached ACCEPTED or later."""
    blocking = ctx.session.execute(
        text(
            "SELECT 1 FROM \"order\" WHERE request_id=:r AND status NOT IN "
            "('PENDING_ACCEPTANCE') LIMIT 1"
        ),
        {"r": ctx.subject_id},
    ).first() is not None
    if blocking:
        return GuardResult.failed("a child Order has already been accepted or progressed further", rule="R-045")
    return GuardResult.passed()


def _effect_cancel_pending_allocations(ctx: Ctx) -> None:
    ctx.session.execute(
        text(
            "DELETE FROM allocation WHERE request_line_id IN "
            "(SELECT id FROM request_line WHERE request_id = :r) AND id NOT IN "
            "(SELECT a.id FROM allocation a JOIN request_line rl ON rl.id = a.request_line_id "
            "JOIN order_line ol ON ol.request_line_id = rl.id)"
        ),
        {"r": ctx.subject_id},
    )


MACHINE = Machine(
    code="SM-01",
    subject_table=SUBJECT,
    status_column="status",
    transitions=[
        # -- catalogue path (checkout) --
        Transition("SM-01", (RequestStatus.DRAFT,), RequestStatus.AWAITING_ADMIN_APPROVAL,
                   "SUBMIT_CART_OVER_THRESHOLD", frozenset({RoleCode.PHARMACY_BUYER}),
                   guard=_guard_submit_over_threshold, effects=_effect_start_admin_approval_timer,
                   rule_refs=("R-030", "R-031", "R-032", "R-122")),
        Transition("SM-01", (RequestStatus.DRAFT,), RequestStatus.CONFIRMED, "SUBMIT_CART",
                   _PHARMACY, guard=_guard_submit_within_threshold, extra_set=lambda ctx: {"confirmed_at": now()},
                   rule_refs=("R-030", "R-031", "R-032", "R-122")),
        Transition("SM-01", (RequestStatus.AWAITING_ADMIN_APPROVAL,), RequestStatus.CONFIRMED, "ADMIN_APPROVE",
                   _ADMIN, guard=_guard_admin_approve, effects=_effect_cancel_admin_approval_timer,
                   extra_set=lambda ctx: {"confirmed_at": now()}, rule_refs=("R-122",)),
        Transition("SM-01", (RequestStatus.AWAITING_ADMIN_APPROVAL,), RequestStatus.DRAFT, "ADMIN_REJECT",
                   frozenset({RoleCode.PHARMACY_ADMIN, "SYSTEM"}), effects=_effect_cancel_admin_approval_timer,
                   rule_refs=("A-142",)),
        # -- RFQ / ASSISTED normalisation-and-confirmation path --
        Transition("SM-01", (RequestStatus.NORMALIZING,), RequestStatus.AWAITING_CONFIRMATION,
                   "NORMALIZATION_COMPLETE", _SYSTEM, effects=_effect_start_confirmation_timer),
        Transition("SM-01", (RequestStatus.AWAITING_CONFIRMATION,), RequestStatus.CONFIRMED, "CONFIRM",
                   _ASSISTED_OR_BUYER, guard=_guard_confirm, effects=_effect_cancel_confirmation_timer,
                   extra_set=lambda ctx: {"confirmed_at": now()}, rule_refs=("R-115",)),
        Transition("SM-01", (RequestStatus.AWAITING_CONFIRMATION,), RequestStatus.NORMALIZING, "REQUEST_CHANGES",
                   _ASSISTED_OR_BUYER, effects=_effect_cancel_confirmation_timer),
        Transition("SM-01", (RequestStatus.AWAITING_CONFIRMATION,), RequestStatus.CANCELLED, "CONFIRMATION_TIMEOUT",
                   _SYSTEM, rule_refs=("A-11",)),
        # -- shared tail (both paths reach CONFIRMED, then converge) --
        Transition("SM-01", (RequestStatus.CONFIRMED,), RequestStatus.IN_FULFILMENT, "FIRST_ORDER_CREATED",
                   frozenset({"SYSTEM"} | _PHARMACY)),
        Transition("SM-01", (RequestStatus.CONFIRMED,), RequestStatus.CANCELLED, "ALL_QUOTATIONS_TERMINAL_NO_ORDER",
                   _SYSTEM, guard=_guard_no_allocations_yet),
        Transition("SM-01", (RequestStatus.IN_FULFILMENT,), RequestStatus.CLOSED, "ALL_ORDERS_TERMINAL",
                   frozenset({"SYSTEM"})),
        Transition("SM-01", (RequestStatus.DRAFT,), RequestStatus.CANCELLED, "ABANDON", _PHARMACY),
        Transition("SM-01", (RequestStatus.CONFIRMED,), RequestStatus.CANCELLED, "CANCEL", _ADMIN,
                   guard=_guard_cancel_before_accepted, effects=_effect_cancel_pending_allocations,
                   rule_refs=("R-045",)),
    ],
)
