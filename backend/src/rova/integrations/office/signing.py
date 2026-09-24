"""HMAC-SHA256 signing shared with PH Office (SPEC 5.3):

    X-PH-Timestamp: <unix seconds>
    X-PH-Signature: v1=<hex(HMAC_SHA256(secret, timestamp + "." + raw_body))>

Verified on the raw bytes before any parse, constant-time, +/-300 s.

Signed requests WITHOUT a body (the GET pull routes, SPEC 5.7) sign the
request line instead of the empty body (SPEC 5.3 v1.1, REVIEW F-4):

    timestamp + "." + METHOD + " " + path + "?" + query

so a captured signature cannot be replayed on another pull route or query
within the 300 s window. Identical to phoffice.core.signing; both sides
ship together."""
import hashlib
import hmac
import time

MAX_SKEW_SECONDS = 300


def canonical_request(method: str, path: str, query: str = "") -> bytes:
    """The signed message of a body-less request: METHOD + " " + path + "?" + query."""
    return f"{method.upper()} {path}?{query or ''}".encode()


def request_canonical(request) -> bytes:
    """canonical_request() of an incoming Starlette/FastAPI request."""
    return canonical_request(request.method, request.url.path, request.url.query)


def sign(secret: str, ts: int | str, body: bytes, *, canonical: bytes | None = None) -> str:
    msg = str(ts).encode() + b"." + (body if canonical is None else canonical)
    return "v1=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def headers(secret: str, body: bytes, event_id: str | None = None, *, canonical: bytes | None = None,
            ts: int | None = None) -> dict:
    ts = int(time.time()) if ts is None else int(ts)
    h = {"X-PH-Timestamp": str(ts), "X-PH-Signature": sign(secret, ts, body, canonical=canonical),
         "Content-Type": "application/json"}
    if event_id:
        h["X-PH-Event-Id"] = event_id
    return h


def get_headers(secret: str, url_path: str, query: str = "", *, method: str = "GET", ts: int | None = None) -> dict:
    """Headers for a signed body-less request to `url_path?query`."""
    return headers(secret, b"", canonical=canonical_request(method, url_path, query), ts=ts)


def verify(secret: str, hdrs, raw_body: bytes, *, canonical: bytes | None = None) -> bool:
    if not secret:
        return False
    ts, sig = hdrs.get("x-ph-timestamp"), hdrs.get("x-ph-signature")
    if not ts or not sig:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > MAX_SKEW_SECONDS:
        return False
    return hmac.compare_digest(sign(secret, ts_int, raw_body, canonical=canonical), sig)
