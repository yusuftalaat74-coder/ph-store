"""A6.1 — the ranking engine is a PURE module: it imports only dataclasses,
decimal, hashlib, json and rova.domain.enums. No fee, agreement, billing or
price-list table or module may ever be reachable from here (R-150, R-022).
`tests/domain/test_ranking_blind.py` asserts this both statically (import
analysis) and dynamically (scrambling fee data changes nothing)."""
import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from rova.domain.enums import FreshnessState


@dataclass(frozen=True)
class Candidate:
    vendor_id: str
    offer_id: str
    freshness_state: str
    qty_available: int
    min_order_qty: int | None
    expiry_horizon_days: int
    price: Decimal | None  # None for regulated products (R-009)
    fill_rate: Decimal
    on_time_dispatch_rate: Decimal
    promise_accuracy: Decimal
    score_value: Decimal
    median_promised_dispatch_hours: Decimal | None
    payment_terms_available: tuple[str, ...]
    credit_days: int | None
    mov_amount: Decimal
    mov_reached: bool
    vendor_status: str = "ACTIVE"


@dataclass(frozen=True)
class RankingInput:
    index_product_id: str
    regulated_price: bool
    qty_requested: int
    pharmacy_id: str
    strategy: str
    candidates: tuple[Candidate, ...]
    excluded_vendor_ids: frozenset[str] = frozenset()  # R-029


@dataclass(frozen=True)
class RankingResult:
    ordered: list[Candidate]
    snapshot: list[dict]
    inputs_fingerprint: str
    single_candidate: bool


def _terms_rank(terms: tuple[str, ...], credit_days: int | None) -> tuple[int, int]:
    if "CREDIT_N_DAYS" in terms:
        return (0, -(credit_days or 0))
    if "COD" in terms:
        return (1, 0)
    return (2, 0)


def _sort_key_free(c: Candidate, qty_requested: int):
    covers = c.qty_available >= qty_requested and (c.min_order_qty is None or qty_requested >= c.min_order_qty)
    combined4 = min(c.on_time_dispatch_rate, c.promise_accuracy)
    hours = c.median_promised_dispatch_hours if c.median_promised_dispatch_hours is not None else Decimal("Infinity")
    terms = _terms_rank(c.payment_terms_available, c.credit_days)
    return (
        not covers,
        c.price if c.price is not None else Decimal("Infinity"),  # R-021: price first, free products only
        -c.fill_rate,
        -combined4,
        hours,
        -c.expiry_horizon_days,
        terms,
        not c.mov_reached,
        -c.score_value,
        c.vendor_id,
    )


def _sort_key_regulated(c: Candidate, qty_requested: int):
    covers = c.qty_available >= qty_requested and (c.min_order_qty is None or qty_requested >= c.min_order_qty)
    combined4 = min(c.on_time_dispatch_rate, c.promise_accuracy)
    hours = c.median_promised_dispatch_hours if c.median_promised_dispatch_hours is not None else Decimal("Infinity")
    terms = _terms_rank(c.payment_terms_available, c.credit_days)
    return (
        not covers,
        -c.fill_rate,
        -combined4,
        hours,
        -c.expiry_horizon_days,
        terms,
        not c.mov_reached,
        -c.score_value,
        c.vendor_id,
    )


def _fingerprint(inp: RankingInput) -> str:
    payload = {
        "index_product_id": inp.index_product_id,
        "regulated_price": inp.regulated_price,
        "qty_requested": inp.qty_requested,
        "pharmacy_id": inp.pharmacy_id,
        "strategy": inp.strategy,
        "candidates": [
            {**{k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(c).items()}}
            for c in inp.candidates
        ],
        "excluded_vendor_ids": sorted(inp.excluded_vendor_ids),
    }
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def rank(inp: RankingInput) -> RankingResult:
    """R-014: only FRESH offers; only vendors not excluded (R-029). R-020/R-021
    order the survivors. Deterministic and side-effect free."""
    fresh = [
        c for c in inp.candidates
        if c.freshness_state == FreshnessState.FRESH and c.vendor_id not in inp.excluded_vendor_ids
        and c.vendor_status in ("ACTIVE", "LICENCE_EXPIRING")
    ]
    key_fn = _sort_key_regulated if inp.regulated_price else _sort_key_free
    ordered = sorted(fresh, key=lambda c: key_fn(c, inp.qty_requested))

    snapshot = []
    for c in ordered:
        snapshot.append({
            "vendor_id": c.vendor_id,
            "offer_id": c.offer_id,
            "price": str(c.price) if c.price is not None else None,
            "fill_rate": str(c.fill_rate),
            "on_time_dispatch_rate": str(c.on_time_dispatch_rate),
            "promise_accuracy": str(c.promise_accuracy),
            "median_promised_dispatch_hours": str(c.median_promised_dispatch_hours) if c.median_promised_dispatch_hours is not None else None,
            "expiry_horizon_days": c.expiry_horizon_days,
            "payment_terms_available": list(c.payment_terms_available),
            "mov_reached": c.mov_reached,
            "score_value": str(c.score_value),
        })
    excluded_ids = {c.vendor_id for c in inp.candidates} - {c.vendor_id for c in fresh}
    for vid in sorted(excluded_ids):
        snapshot.append({"vendor_id": vid, "filtered": True})

    return RankingResult(
        ordered=ordered,
        snapshot=snapshot,
        inputs_fingerprint=_fingerprint(inp),
        single_candidate=len(fresh) == 1,
    )
