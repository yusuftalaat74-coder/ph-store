"""A11.2 — schedule selection at Order creation, by scope precedence
VENDOR/PHARMACY > REGION > GLOBAL, restricted to families enabled by the
resolved FEE_MODEL."""
from sqlalchemy import text
from sqlalchemy.orm import Session

_FAMILY_OF = {
    "VENDOR_SHARE": "VENDOR_SHARE",
    "PHARMACY_SERVICE_FEE": "PHARMACY_SERVICE_FEE",
    "PER_DELIVERY_JOB": "DELIVERY_FEE",
}


def _enabled_families(session: Session) -> set[str]:
    current = session.execute(
        text("SELECT current_value FROM mode_switch WHERE switch_key='FEE_MODEL' AND scope_type='GLOBAL'")
    ).scalar()
    return set((current or "").split(",")) if current else set()


def active_schedules_for(session: Session, *, vendor_id: str, pharmacy_id: str, region_code: str, today) -> list[dict]:
    enabled = _enabled_families(session)
    rows = session.execute(
        text(
            "SELECT * FROM fee_schedule WHERE effective_from <= :today "
            "AND (effective_to IS NULL OR effective_to >= :today)"
        ),
        {"today": today},
    ).mappings().all()

    picked: dict[tuple[str, str], dict] = {}
    for row in rows:
        family = _FAMILY_OF.get(row["type"])
        if family is not None and family not in enabled:
            continue
        key = (row["type"], row["payer"])
        scope_rank = {"VENDOR": 0, "PHARMACY": 0, "REGION": 1, "GLOBAL": 2}[row["scope_type"]]
        if row["payer"] == "VENDOR":
            if row["scope_type"] == "VENDOR" and row["scope_id"] != vendor_id:
                continue
            if row["scope_type"] == "REGION" and row["scope_id"] != region_code:
                continue
        elif row["payer"] == "PHARMACY":
            if row["scope_type"] == "PHARMACY" and row["scope_id"] != pharmacy_id:
                continue
            if row["scope_type"] == "REGION" and row["scope_id"] != region_code:
                continue
        existing = picked.get(key)
        if existing is None or scope_rank < existing["_rank"]:
            picked[key] = {**dict(row), "_rank": scope_rank}
    return list(picked.values())
