"""SM-12 — vendor_offer (A4.2 row SM-12)."""
from rova.core.clock import now
from rova.domain.enums import FreshnessState, RoleCode
from rova.domain.fsm import Machine, Transition

_VENDOR = frozenset({RoleCode.VENDOR_ADMIN, RoleCode.VENDOR_ORDER_DESK})


MACHINE = Machine(
    code="SM-12",
    subject_table="vendor_offer",
    status_column="freshness_state",
    transitions=[
        Transition("SM-12", (FreshnessState.FRESH,), FreshnessState.STALE, "GO_STALE",
                   frozenset({"SYSTEM"})),
        # RECONFIRM is a self-loop that also refreshes stock_confirmed_at (R-019).
        Transition("SM-12", (FreshnessState.FRESH, FreshnessState.STALE), FreshnessState.FRESH, "RECONFIRM",
                   _VENDOR, extra_set=lambda ctx: {"stock_confirmed_at": now()}, rule_refs=("R-019",)),
        # SYSTEM added (additive) so the price-list publisher (A12.2 step 6,
        # R-156) can withdraw a vendor's offers absent from a non-partial
        # version without impersonating a vendor user.
        Transition("SM-12", (FreshnessState.FRESH, FreshnessState.STALE), FreshnessState.WITHDRAWN, "WITHDRAW",
                   _VENDOR | frozenset({"SYSTEM"})),
        Transition("SM-12", (FreshnessState.FRESH, FreshnessState.STALE), FreshnessState.WITHDRAWN, "QTY_ZERO",
                   frozenset({"SYSTEM"} | _VENDOR), rule_refs=("R-018",)),
    ],
)
