"""A15.3 — Idempotency-Key handling for [idem] endpoints.

B fix 1c: the hash below is, and already was, sensitive to `body` — a
genuinely different `body` dict on the same `(key, principal_id)` does
correctly raise `IDEMPOTENCY_CONFLICT`. The defect the review's curl proof
caught lives one layer up, at each call site: `rova/ordering/router.py`'s
checkout endpoint passes a hardcoded `body={}` to both functions below
regardless of what the client actually sent, because the endpoint has no
request-body model at all — so no caller-observable body variation was ever
possible to hash in the first place, and the module looked broken even
though the hashing itself was sound. Fixing that call site is out of this
session's scope (it sits in `rova/ordering/router.py`, which another
executor is rebuilding in parallel as part of the order/billing loop); see
`tests/domain/test_idempotency.py` for this module's fix proven directly
against the real conflict/replay behaviour with an actual differing body,
and `tests/api/test_checkout_idempotency.py::test_same_key_different_body_conflicts`
for why the HTTP-level version of that same test could only ever vary the
URL path, not the body, given that endpoint's current contract.

Two real fixes do live in this module: `request_hash` now uses a properly
canonical (compact-separator) encoding rather than relying on Python's
default `json.dumps` spacing, and `check_and_store` can now be handed a
FastAPI `Response` to set `Idempotency-Replayed: true` on, which no caller
did before (the header was simply never emitted, on any code path)."""
import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.core.errors import ApiError


def request_hash(method: str, path: str, body: dict) -> str:
    # canonical form (A2.4-style "computed once, the same way everywhere"):
    # sorted keys, no incidental whitespace, so two dicts that are equal as
    # data always hash equal regardless of key insertion order.
    raw = method + "|" + path + "|" + json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def check_and_store(
    session: Session, *, key: str | None, principal_id: str, method: str, path: str, body: dict, response=None,
) -> dict | None:
    """Returns a stored response dict to replay, or None to proceed and then
    call `store()`. Raises 422 if the key is missing, 409 on hash conflict
    (a different canonical `body` reusing the same key — the real request
    body, not a caller-supplied stand-in, is what must be passed here for
    this guarantee to mean anything).

    `response`, when given a FastAPI/Starlette `Response` (e.g. via a
    `response: Response` endpoint parameter), gets `Idempotency-Replayed:
    true` set on a genuine replay, per A17's checkout-idempotency row."""
    if not key or not (1 <= len(key) <= 128):
        raise ApiError("VALIDATION_ERROR", "Idempotency-Key header is required for this endpoint")
    h = request_hash(method, path, body)
    row = session.execute(
        text("SELECT request_hash, status_code, response_body FROM idempotency_key WHERE key=:k AND principal_id=:p"),
        {"k": key, "p": principal_id},
    ).mappings().first()
    if row is None:
        return None
    if row["request_hash"] != h:
        raise ApiError("IDEMPOTENCY_CONFLICT", "Idempotency-Key reused with a different request body")
    if response is not None:
        response.headers["Idempotency-Replayed"] = "true"
    body_out = row["response_body"]
    return {"status_code": row["status_code"], "body": body_out, "replayed": True}


def store(session: Session, *, key: str, principal_id: str, method: str, path: str, body: dict, status_code: int, response_body: dict) -> None:
    h = request_hash(method, path, body)
    session.execute(
        text(
            "INSERT INTO idempotency_key (key, principal_id, request_hash, status_code, response_body) "
            "VALUES (:k, :p, :h, :s, CAST(:b AS JSONB)) ON CONFLICT (key, principal_id) DO NOTHING"
        ),
        {"k": key, "p": principal_id, "h": h, "s": status_code, "b": json.dumps(response_body, default=str)},
    )
