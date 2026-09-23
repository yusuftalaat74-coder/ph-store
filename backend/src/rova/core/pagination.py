"""A15.1 — cursor pagination: ?limit= (default 50, max 100 [A-BE-04]),
?cursor= opaque base64 of (created_at, id)."""
import base64
import json
from datetime import datetime

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def encode_cursor(created_at: datetime, id_: str) -> str:
    payload = json.dumps([created_at.isoformat(), id_]).encode()
    return base64.urlsafe_b64encode(payload).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    created_at_s, id_ = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    return datetime.fromisoformat(created_at_s), id_


def page_envelope(items: list, next_cursor: str | None) -> dict:
    return {"items": items, "next_cursor": next_cursor}
