"""SM-21 — price_list_version (A4.2 row SM-21, A12). The pipeline itself
(parse/fingerprint/map/validate/publish/go-live) lives in `rova/pricelist/`;
this module only fixes the legal state graph and who may drive it."""
from rova.core.clock import now
from rova.domain.enums import PriceListVersionStatus, RoleCode
from rova.domain.fsm import Machine, Transition

_VENDOR = frozenset({RoleCode.VENDOR_ADMIN, RoleCode.VENDOR_ORDER_DESK})
_VENDOR_ADMIN_ONLY = frozenset({RoleCode.VENDOR_ADMIN})
_OPS = frozenset({RoleCode.OPS_REVIEWER})
_SYSTEM = frozenset({"SYSTEM"})

S = PriceListVersionStatus

MACHINE = Machine(
    code="SM-21",
    subject_table="price_list_version",
    status_column="status",
    transitions=[
        # automatic, decided by whether an ACTIVE vendor_column_mapping
        # already matches this file's fingerprint (A12.2 step 3)
        Transition("SM-21", (S.UPLOADED,), S.VALIDATING, "MAPPING_FOUND", _SYSTEM),
        Transition("SM-21", (S.UPLOADED,), S.MAPPING_NEEDED, "MAPPING_NEEDED", _SYSTEM),
        # a human confirms/corrects the mapping once (§2); VendorAdmin owns
        # the vendor's mapping memory, OpsReviewer maps on the vendor's
        # behalf (OP-43)
        Transition("SM-21", (S.MAPPING_NEEDED,), S.VALIDATING, "MAPPING_SAVED", _VENDOR_ADMIN_ONLY | _OPS),
        # automatic, decided by the validator's counters (A12.2 step 4)
        Transition("SM-21", (S.VALIDATING,), S.VALIDATED, "VALIDATED", _SYSTEM),
        Transition("SM-21", (S.VALIDATING,), S.REJECTED, "ALL_REJECTED", _SYSTEM),
        # give up before a mapping/validation ever completed
        Transition("SM-21", (S.MAPPING_NEEDED,), S.REJECTED, "ABANDON", _VENDOR_ADMIN_ONLY | _OPS),
        Transition("SM-21", (S.VALIDATED,), S.SCHEDULED, "PUBLISH", _VENDOR,
                   extra_set=lambda ctx: {"effective_from": ctx.kwargs["effective_from"]}),
        Transition("SM-21", (S.SCHEDULED,), S.LIVE, "GO_LIVE", _SYSTEM,
                   extra_set=lambda ctx: {"went_live_at": now()}),
        Transition("SM-21", (S.VALIDATED,), S.REJECTED, "DISCARD", _VENDOR_ADMIN_ONLY),
        Transition("SM-21", (S.SCHEDULED,), S.REJECTED, "CANCEL_SCHEDULED", _VENDOR_ADMIN_ONLY),
        Transition("SM-21", (S.LIVE,), S.SUPERSEDED, "SUPERSEDE", _SYSTEM),
    ],
)
