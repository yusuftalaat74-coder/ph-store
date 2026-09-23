"""SM-10 — "return" (E-036, A4.2 row SM-10). `CLOSE_WITH_CREDIT_NOTE`'s
credit_note + ledger_entry creation (R-083) is done by
`rova/billing/returns.py` before this transition is applied (same
transaction), then this machine only records the state change — a machine
module never writes another domain's tables itself (A4.1)."""
from rova.domain.enums import ReturnStatus, RoleCode
from rova.domain.fsm import Machine, Transition

_VF = frozenset({RoleCode.VENDOR_FINANCE, RoleCode.VENDOR_ADMIN})
_DI = frozenset({RoleCode.DISPATCHER})

S = ReturnStatus

MACHINE = Machine(
    code="SM-10",
    subject_table="return",
    status_column="status",
    transitions=[
        Transition("SM-10", (S.REQUESTED,), S.APPROVED, "APPROVE", _VF),
        Transition("SM-10", (S.REQUESTED,), S.REJECTED, "REJECT", _VF),
        Transition("SM-10", (S.APPROVED,), S.GOODS_IN_TRANSIT, "SHIP", _DI),
        Transition("SM-10", (S.GOODS_IN_TRANSIT,), S.RECEIVED_BY_VENDOR, "RECEIVE", _VF),
        Transition("SM-10", (S.RECEIVED_BY_VENDOR,), S.CLOSED, "CLOSE_WITH_CREDIT_NOTE", _VF),
    ],
)
