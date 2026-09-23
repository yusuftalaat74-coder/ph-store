"""A5 — CFG-* resolution with scope precedence VENDOR/PHARMACY > REGION > GLOBAL.
Business parameters are never environment variables (A2.10); they live in
`config_parameter`, seeded by `rova seed --config-only` (A16). Writes go
through `PUT /v1/config/{key}` only (see `rova/config_params/router.py`)."""
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.core.errors import ApiError

_CASTERS = {
    "INT": int,
    "DECIMAL": Decimal,
    "BOOL": lambda v: v.strip().lower() in ("true", "1", "yes"),
    "ENUM": str,
    "TEXT": str,
}


def _cast(value: str, value_type: str):
    return _CASTERS.get(value_type, str)(value)


def get(session: Session, key: str, *, vendor_id: str | None = None, region_code: str | None = None,
        pharmacy_id: str | None = None, default=None):
    """VENDOR (or PHARMACY, reusing the VENDOR scope_type slot is not
    modelled separately in A3 — config_parameter only has VENDOR/REGION/
    GLOBAL — so a pharmacy-scoped override is not resolvable here and this
    backend has none seeded) -> REGION -> GLOBAL. A missing GLOBAL row is a
    startup error unless `default` is supplied, in which case the caller's
    documented default stands in (kept out of the DB rather than guessing a
    business value that was never specified — A2.10)."""
    row = None
    if vendor_id:
        row = session.execute(
            text("SELECT value, value_type FROM config_parameter WHERE key=:k AND scope_type='VENDOR' AND scope_id=:s"),
            {"k": key, "s": vendor_id},
        ).mappings().first()
    if row is None and region_code:
        row = session.execute(
            text("SELECT value, value_type FROM config_parameter WHERE key=:k AND scope_type='REGION' AND scope_id=:s"),
            {"k": key, "s": region_code},
        ).mappings().first()
    if row is None:
        row = session.execute(
            text("SELECT value, value_type FROM config_parameter WHERE key=:k AND scope_type='GLOBAL'"),
            {"k": key},
        ).mappings().first()
    if row is None:
        if default is not None:
            return default
        raise ApiError("INTERNAL", f"missing GLOBAL config_parameter row for {key}")
    return _cast(row["value"], row["value_type"])


def get_owner_role(session: Session, key: str) -> str | None:
    row = session.execute(
        text("SELECT owner_role FROM config_parameter WHERE key=:k AND scope_type='GLOBAL'"), {"k": key}
    ).mappings().first()
    return row["owner_role"] if row else None
