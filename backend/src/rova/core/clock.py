"""now(), injectable for tests (A2.14 core/clock.py)."""
from datetime import datetime, timezone

_override: datetime | None = None


def now() -> datetime:
    if _override is not None:
        return _override
    return datetime.now(timezone.utc)


def freeze(at: datetime) -> None:
    global _override
    _override = at


def unfreeze() -> None:
    global _override
    _override = None
