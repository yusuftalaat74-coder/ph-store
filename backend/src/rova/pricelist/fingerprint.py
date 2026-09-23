"""A12.2 step 2 — `sha256(json.dumps([normalise(h) for h in headers] + [len(headers)]))`.
Two files from the same vendor with the same header shape (any order the
caller passes them in, since headers are supplied in file order) hash
identically, which is what lets a saved `vendor_column_mapping` be reused
"forever, in his format" (§2 of مواصفة_استيراد_قائمة_الأسعار.md)."""
import hashlib
import json
import re
import unicodedata

_TRAILING_STAR_RE = re.compile(r"\s*\*\s*$")
_WS_RE = re.compile(r"\s+")


def normalise_header(h: str) -> str:
    h = str(h or "")
    h = _TRAILING_STAR_RE.sub("", h)
    h = unicodedata.normalize("NFKD", h)
    h = "".join(c for c in h if not unicodedata.combining(c))
    h = h.strip().lower()
    h = _WS_RE.sub(" ", h)
    return h


def fingerprint(headers: list[str]) -> str:
    normalised = [normalise_header(h) for h in headers]
    payload = json.dumps(normalised + [len(headers)], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
