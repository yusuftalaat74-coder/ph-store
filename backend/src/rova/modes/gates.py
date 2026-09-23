"""§A5 gates for `SM-22 ModeSwitch: PROPOSED -> GATE_CHECKING -> READY`.

Implemented subset: `G4` (`count(vendor_account.status='ACTIVE') >= 2`) for
`VENDOR_MODE -> MULTI` — the one gate the seed fixture is built to
demonstrate failing (`ven_mozpharma` deliberately left `ONBOARDING`, per
`rova/seed/seed.py`'s own comment). `G1`-`G3`/`G5`-`G8` (ranking-isolation
audit freshness, fee-schedule/agreement checks, the R-141 text-review audit
event, vendor-score coverage, the multi-vendor terms publication window) and
`F1`/`F2`/`P1` for the other two switch keys are not wired in this session
(reduced scope) — every switch/target other than `VENDOR_MODE -> MULTI`
evaluates as "no gates named" and passes trivially, which is a documented
cut, not a silent pass dressed up as a real check."""
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    passed: bool
    evidence_ref: str
    detail: str = ""


def evaluate_g4(session: Session) -> GateResult:
    count = session.execute(
        text("SELECT count(*) FROM vendor_account WHERE status='ACTIVE'")
    ).scalar()
    return GateResult(
        gate_id="G4", passed=count >= 2,
        evidence_ref=f"vendor_account ACTIVE count = {count}",
        detail="" if count >= 2 else f"only {count} ACTIVE vendor(s); at least 2 required",
    )


def evaluate_gates(session: Session, switch_key: str, proposed_value: str) -> list[GateResult]:
    if switch_key == "VENDOR_MODE" and proposed_value == "MULTI":
        return [evaluate_g4(session)]
    return []
