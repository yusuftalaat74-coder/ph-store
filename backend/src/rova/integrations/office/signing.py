"""HMAC-SHA256 signing shared with PH Office (SPEC 5.3):

    X-PH-Timestamp: <unix seconds>
    X-PH-Signature: v1=<hex(HMAC_SHA256(secret, timestamp + "." + raw_body))>

Verified on the raw bytes before any parse, constant-time, +/-300 s."""
import hashlib
import hmac
import time

MAX_SKEW_SECONDS = 300


def sign(secret: str, ts: int | str, body: bytes) -> str:
    return "v1=" + hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()


def headers(secret: str, body: bytes, event_id: str | None = None) -> dict:
    ts = int(time.time())
    h = {"X-PH-Timestamp": str(ts), "X-PH-Signature": sign(secret, ts, body), "Content-Type": "application/json"}
    if event_id:
        h["X-PH-Event-Id"] = event_id
    return h


def verify(secret: str, hdrs, raw_body: bytes) -> bool:
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
    return hmac.compare_digest(sign(secret, ts_int, raw_body), sig)
