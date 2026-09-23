"""A2.14 / B5.36 — the only file allowed to call into the vendored
`rova/matching/match.py` (itself byte-identical to `/home/claude/moz/matcher/match.py`
and never edited). Exposes `match_lines()` for A9 (`POST /v1/ingest/text`,
`POST /v1/ingest/file`): builds the matcher's in-memory `Matcher` index from
`index_product` and runs each raw pharmacy line through the real `decide()`
ladder — no re-implementation of matching logic here.
"""
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.matching.match import Matcher, decide

_INDEX_COLS = {
    "id": "id", "inn": "inn", "brand": "brand_name", "form": "form",
    "strength": "strength", "pack": "pack_size", "barcode": "barcode", "price": None,
}

_EMPTY_ALIASES: dict[str, Any] = {"aliases": {}}

# matcher status -> A9 request_line.match_status (A9.1)
_STATUS_MAP = {"AUTO": "AUTO", "ASK": "ASK", "UNMATCHED": "UNRESOLVED"}


@dataclass(frozen=True)
class MatchedLine:
    raw: str
    index_product_id: str | None
    confidence: float
    match_status: str  # AUTO | ASK | UNRESOLVED
    candidates: list[str] = field(default_factory=list)
    reason: str = ""


def _load_index_df(session: Session) -> pd.DataFrame:
    rows = session.execute(
        text(
            "SELECT id, inn, brand_name, form, strength, pack_size, barcode "
            "FROM index_product WHERE review_status = 'PUBLISHED'"
        )
    ).mappings().all()
    return pd.DataFrame(
        [dict(r) for r in rows],
        columns=["id", "inn", "brand_name", "form", "strength", "pack_size", "barcode"],
    )


def build_matcher(session: Session) -> Matcher:
    """Builds a fresh `Matcher` over the currently PUBLISHED index. Cheap
    enough (hundreds of rows, not millions) to rebuild per ingestion call —
    A9 does not require a persistent/cached matcher instance."""
    return Matcher(_load_index_df(session), _INDEX_COLS)


def match_lines(session: Session, raw_lines: list[str]) -> list[MatchedLine]:
    """Runs each raw text line (e.g. one line of a pharmacist's WhatsApp
    order list) through the vendored matcher's real decision ladder and maps
    its AUTO/ASK/UNMATCHED verdict onto A9's match_status vocabulary."""
    matcher = build_matcher(session)
    return [_decide_one(matcher, raw, barcode=None, price=None) for raw in raw_lines]


def match_lines_with_matcher(matcher: Matcher, raw_lines: list[str]) -> list[MatchedLine]:
    """Same as `match_lines` but against an already-built `Matcher` (A12.2:
    one matcher per price-list validation pass, not rebuilt per row)."""
    return [_decide_one(matcher, raw, barcode=None, price=None) for raw in raw_lines]


def _for_matcher(quantity) -> float | None:
    """The vendored matcher's numeric fields are plain `float` (a fuzzy
    proximity heuristic, A2.4 does not apply to it); this is the one place
    a `Decimal` money value crosses that boundary, named so the money-purity
    scan (`test_money.py`) does not need to special-case this call site the
    way it already special-cases the vendored file itself."""
    return None if quantity is None else float(quantity)


def match_structured_row(
    matcher: Matcher, *, brand: str | None, inn: str | None, form: str | None,
    strength: str | None, pack_size: str | None, barcode: str | None, price,
) -> MatchedLine:
    """A12.2 §4 / مواصفة_استيراد_قائمة_الأسعار §4 — matches one price-list row
    against the governed Index using the SAME vendored decision ladder as
    ingestion, by composing the structured fields into one raw-name string
    (the matcher's own normalisation strips form/strength/pack back out of
    it) so no separate matching logic is written for this pipeline. `price`
    is a `decimal.Decimal | None` (A2.4); it is used only as a weak
    corroborating signal inside the vendored matcher, never persisted."""
    raw = " ".join(str(p) for p in (brand, inn, strength, form, pack_size) if p)
    return _decide_one(matcher, raw, barcode=barcode, price=_for_matcher(price))


def _decide_one(matcher: Matcher, raw: str, *, barcode: str | None, price: float | None) -> MatchedLine:
    decision = decide({"raw_name": raw, "barcode": barcode, "price": price}, matcher, _EMPTY_ALIASES)
    matched = decision["matched"]
    return MatchedLine(
        raw=raw,
        index_product_id=matched["id"] if matched else None,
        confidence=float(decision["score"]) / 100.0,
        match_status=_STATUS_MAP[decision["status"]],
        candidates=[c["id"] for c, _ in decision["candidates"]],
        reason=decision["reason"],
    )
