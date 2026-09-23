"""A12.2 step 4 (`validator.py`) — per-row validation in the exact R-154
order (first failure wins), then matching against the governed Index via
the vendored matcher (`rova.matching.adapter`, never re-implemented here).
Behavioural detail on top of A12 (price-ordering sanity check, past-expiry
rejection, discount range) is drawn from
مواصفة_استيراد_قائمة_الأسعار.md §3/§4/§8, folded into the same MISSING_REQUIRED
/ INVALID_NUMBER outcomes A12.2 names for "unreadable number / missing
required" (step 1) rather than inventing a new outcome_reason value.
"""
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.config_params import service as cfg
from rova.core.clock import now
from rova.matching.adapter import build_matcher, match_structured_row

_STOCK_STATUS_ZERO = {"sem stock"}
_STOCK_STATUS_KNOWN = {"em stock", "sem stock", "sob encomenda"}


@dataclass
class RowResult:
    row_index: int
    raw_values: dict
    vendor_sku: str | None = None
    index_product_id: str | None = None
    match_confidence: float | None = None
    candidate_product_ids: list = field(default_factory=list)
    price_to_pharmacy: Decimal | None = None
    price_to_public: Decimal | None = None
    discount_pct: Decimal | None = None
    vendor_cost: Decimal | None = None
    qty_available: int | None = None
    pack_size: str | None = None
    expiry_horizon_days: int | None = None
    min_order_qty: int | None = None
    outcome: str = "ACCEPTED"
    outcome_reason: str | None = None
    outcome_detail: str | None = None
    regulated_price: bool | None = None
    _dup_key: tuple | None = None


def _reject(rr: RowResult, reason: str, detail: str) -> RowResult:
    rr.outcome, rr.outcome_reason, rr.outcome_detail = "REJECTED", reason, detail
    return rr


def _s(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _num(v) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float, Decimal)):
        try:
            return Decimal(str(v))
        except InvalidOperation:
            return None
    s = str(v).strip().replace(" ", "")
    s = s.replace(",", ".") if s.count(",") == 1 and s.count(".") == 0 else s.replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _date(v) -> date | None:
    """Raw cell values are round-tripped through JSONB storage (§ upload
    endpoint), so an Excel datetime arrives here as its ISO string, not a
    live `datetime` — `fromisoformat` is tried first for exactly that case,
    then a couple of common vendor-typed formats."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _apply_mapping(raw_row: dict, mapping: dict[str, str | None]) -> dict:
    return {field: raw_row.get(header) for field, header in mapping.items() if header}


def _validate_fields(rr: RowResult, mapped: dict, today: date) -> bool:
    """Step 1 (R-154): missing required / unreadable number. Returns False
    (rr already marked REJECTED) if the row cannot proceed to matching."""
    inn, brand = _s(mapped.get("product_name_inn")), _s(mapped.get("product_name_brand"))
    if not inn and not brand:
        _reject(rr, "MISSING_REQUIRED", "falta product_name_inn e product_name_brand (pelo menos um é obrigatório)")
        return False

    pack_size = _s(mapped.get("pack_size"))
    if not pack_size:
        _reject(rr, "MISSING_REQUIRED", "falta pack_size")
        return False
    pack_num = _num(pack_size)
    if pack_num is None or pack_num <= 0 or pack_num != pack_num.to_integral_value():
        _reject(rr, "INVALID_NUMBER", "pack_size deve ser um número inteiro positivo")
        return False
    rr.pack_size = str(int(pack_num))

    price_to_pharmacy = _num(mapped.get("price_to_pharmacy"))
    if mapped.get("price_to_pharmacy") in (None, "") :
        _reject(rr, "MISSING_REQUIRED", "falta price_to_pharmacy")
        return False
    if price_to_pharmacy is None or price_to_pharmacy < 0:
        _reject(rr, "INVALID_NUMBER", "price_to_pharmacy não é um número válido")
        return False
    rr.price_to_pharmacy = price_to_pharmacy

    stock_status = (_s(mapped.get("stock_status")) or "").lower()
    qty_raw = mapped.get("available_quantity")
    if qty_raw in (None, "") and not stock_status:
        _reject(rr, "MISSING_REQUIRED", "falta available_quantity ou stock_status")
        return False
    qty_num = _num(qty_raw)
    if qty_raw not in (None, "") and (qty_num is None or qty_num < 0):
        _reject(rr, "INVALID_NUMBER", "available_quantity não é um número válido")
        return False
    if stock_status and stock_status not in _STOCK_STATUS_KNOWN:
        _reject(rr, "INVALID_NUMBER", f"stock_status '{stock_status}' não reconhecido")
        return False
    if qty_num is not None:
        rr.qty_available = int(qty_num)
    elif stock_status in _STOCK_STATUS_ZERO:
        rr.qty_available = 0
    else:
        rr.qty_available = 0  # "Sob encomenda"/"Em stock" without a number: R-154 leaves this to the vendor's next file (no invented value)

    price_to_public = _num(mapped.get("price_to_public"))
    if mapped.get("price_to_public") not in (None, ""):
        if price_to_public is None or price_to_public < 0:
            _reject(rr, "INVALID_NUMBER", "price_to_public não é um número válido")
            return False
        rr.price_to_public = price_to_public
        if price_to_pharmacy > price_to_public:
            _reject(rr, "INVALID_NUMBER", "price_to_pharmacy > price_to_public — sem margem para a farmácia")
            return False

    discount = _num(mapped.get("discount_percent"))
    if mapped.get("discount_percent") not in (None, ""):
        if discount is None:
            _reject(rr, "INVALID_NUMBER", "discount_percent não é um número válido")
            return False
        if discount > 1:
            discount = discount / Decimal(100)  # tolerate "5" meaning 5%
        if discount < 0 or discount > Decimal("0.5"):
            _reject(rr, "INVALID_NUMBER", "discount_percent fora do intervalo permitido (0%-50%)")
            return False
        rr.discount_pct = discount

    cost = _num(mapped.get("cost_to_us"))
    if cost is not None:
        rr.vendor_cost = cost

    min_oq = _num(mapped.get("min_order_qty"))
    if min_oq is not None and min_oq > 0:
        rr.min_order_qty = int(min_oq)

    expiry = _date(mapped.get("expiry_date"))
    if mapped.get("expiry_date") not in (None, "") and expiry is None:
        _reject(rr, "INVALID_NUMBER", "expiry_date não é uma data válida")
        return False
    if expiry is not None:
        if expiry < today:
            _reject(rr, "INVALID_NUMBER", "expiry_date está no passado")
            return False
        rr.expiry_horizon_days = (expiry - today).days

    rr.vendor_sku = _s(mapped.get("vendor_sku"))
    rr._brand, rr._inn = brand, inn
    rr._form, rr._strength = _s(mapped.get("form")), _s(mapped.get("strength"))
    rr._barcode = _s(mapped.get("barcode"))
    return True


def validate_version(session: Session, version_id: str) -> dict:
    """Runs the whole pipeline for every raw row already stored on this
    version (inserted by the upload endpoint with outcome='PENDING_REVIEW'
    as a placeholder) and rewrites each row's outcome/outcome_reason plus
    the version's counters. Returns {"accepted":n, "rejected":n, "warning":n,
    "pending":n, "all_rejected": bool}."""
    version = session.execute(
        text("SELECT * FROM price_list_version WHERE id=:v"), {"v": version_id}
    ).mappings().one()
    mapping_row = session.execute(
        text("SELECT mapping FROM vendor_column_mapping WHERE id=:m"), {"m": version["mapping_id"]}
    ).mappings().one()
    mapping: dict[str, str | None] = mapping_row["mapping"]

    rows = session.execute(
        text("SELECT id, row_index, raw_values FROM price_list_row WHERE version_id=:v ORDER BY row_index"),
        {"v": version_id},
    ).mappings().all()

    today = now().date()
    grace_days = cfg.get(session, "CFG-NEAR-EXPIRY-DAYS", default=90)
    matcher = build_matcher(session)

    results: dict[str, RowResult] = {}
    dup_index: dict[tuple, list[str]] = {}

    for row in rows:
        rr = RowResult(row_index=row["row_index"], raw_values=row["raw_values"], regulated_price=None)
        mapped = _apply_mapping(row["raw_values"], mapping)
        if not _validate_fields(rr, mapped, today):
            results[row["id"]] = rr
            continue

        matched = match_structured_row(
            matcher, brand=rr._brand, inn=rr._inn, form=rr._form, strength=rr._strength,
            pack_size=rr.pack_size, barcode=rr._barcode, price=rr.price_to_pharmacy,
        )
        rr.match_confidence = matched.confidence
        rr.candidate_product_ids = matched.candidates

        if matched.match_status == "ASK":
            rr.outcome, rr.outcome_reason = "PENDING_REVIEW", "AMBIGUOUS_MATCH"
            rr.outcome_detail = matched.reason
            results[row["id"]] = rr
            continue
        if matched.match_status == "UNRESOLVED" or matched.index_product_id is None:
            _reject(rr, "UNMATCHED_PRODUCT", matched.reason or "nenhum produto correspondente encontrado no Índice")
            results[row["id"]] = rr
            continue

        product = session.execute(
            text("SELECT * FROM index_product WHERE id=:p"), {"p": matched.index_product_id}
        ).mappings().one()
        rr.index_product_id = product["id"]

        if product["review_status"] != "PUBLISHED":
            _reject(rr, "PRODUCT_NOT_PUBLISHED", "produto correspondente não está publicado no Índice")
            results[row["id"]] = rr
            continue
        if product["aim_status"] != "AUTHORISED":
            _reject(rr, "NOT_AUTHORISED", "produto sem autorização de introdução no mercado (AIM) válida")
            results[row["id"]] = rr
            continue

        rr.regulated_price = bool(product["regulated_price"])
        if rr.regulated_price:
            if rr.discount_pct and rr.discount_pct > 0:
                _reject(rr, "DISCOUNT_ON_REGULATED",
                        "desconto não permitido sobre produto de preço regulado (Art. 64(2)(a))")
                results[row["id"]] = rr
                continue
            ref = session.execute(
                text(
                    "SELECT * FROM price_reference WHERE index_product_id=:p AND effective_from <= :today "
                    "ORDER BY effective_from DESC LIMIT 1"
                ),
                {"p": product["id"], "today": today},
            ).mappings().first()
            if ref is None:
                _reject(rr, "REGULATED_PRICE_DEVIATION", "sem preço oficial registado")
                results[row["id"]] = rr
                continue
            if rr.price_to_pharmacy != ref["wholesale_derived_price"]:
                _reject(rr, "REGULATED_PRICE_DEVIATION",
                        f"price_to_pharmacy ({rr.price_to_pharmacy}) difere do preço grossista oficial "
                        f"({ref['wholesale_derived_price']})")
                results[row["id"]] = rr
                continue
            if rr.price_to_public is not None and rr.price_to_public != ref["pvp_price"]:
                rr.outcome, rr.outcome_reason = "WARNING", "PVP_DEVIATION_WARNING"
                rr.outcome_detail = f"price_to_public ({rr.price_to_public}) difere do PVP oficial ({ref['pvp_price']})"

        if str(product["pack_size"]).strip() != rr.pack_size:
            _reject(rr, "PACK_SIZE_MISMATCH",
                    f"pack_size do ficheiro ({rr.pack_size}) difere do Índice ({product['pack_size']})")
            results[row["id"]] = rr
            continue

        if rr.expiry_horizon_days is None:
            rr.expiry_horizon_days = grace_days
            note = f"sem data de validade; assumido CFG-NEAR-EXPIRY-DAYS={grace_days} dias"
            rr.outcome_detail = f"{rr.outcome_detail}; {note}" if rr.outcome_detail else note

        key = (rr.index_product_id, rr.pack_size)
        dup_index.setdefault(key, []).append(row["id"])
        results[row["id"]] = rr

    # rule 6: second row for the same (product, pack) -> the EARLIER row
    # becomes REJECTED/DUPLICATE_ROW; the latest one in file order stands.
    for key, ids in dup_index.items():
        if len(ids) < 2:
            continue
        for rid in ids[:-1]:
            rr = results[rid]
            if rr.outcome in ("ACCEPTED", "WARNING"):
                rr.outcome, rr.outcome_reason = "REJECTED", "DUPLICATE_ROW"
                rr.outcome_detail = "linha substituída por outra linha posterior no mesmo ficheiro para o mesmo produto/embalagem"

    accepted = rejected = warning = pending = 0
    for row_id, rr in results.items():
        if rr.outcome == "ACCEPTED":
            accepted += 1
        elif rr.outcome == "REJECTED":
            rejected += 1
        elif rr.outcome == "WARNING":
            warning += 1
        else:
            pending += 1
        session.execute(
            text(
                "UPDATE price_list_row SET vendor_sku=:sku, index_product_id=:pid, match_confidence=:conf, "
                "candidate_product_ids=CAST(:cand AS JSONB), price_to_pharmacy=:ptp, price_to_public=:ptpub, "
                "discount_pct=:disc, vendor_cost=:cost, qty_available=:qty, pack_size=:pack, "
                "expiry_horizon_days=:exp, min_order_qty=:moq, outcome=:out, outcome_reason=:reason, "
                "outcome_detail=:detail, updated_at=:n WHERE id=:id"
            ),
            {
                "sku": rr.vendor_sku, "pid": rr.index_product_id, "conf": rr.match_confidence,
                "cand": json.dumps(rr.candidate_product_ids), "ptp": rr.price_to_pharmacy, "ptpub": rr.price_to_public,
                "disc": rr.discount_pct, "cost": rr.vendor_cost, "qty": rr.qty_available, "pack": rr.pack_size,
                "exp": rr.expiry_horizon_days, "moq": rr.min_order_qty, "out": rr.outcome, "reason": rr.outcome_reason,
                "detail": rr.outcome_detail, "n": now(), "id": row_id,
            },
        )

    session.execute(
        text(
            "UPDATE price_list_version SET accepted_rows=:a, rejected_rows=:r, warning_rows=:w, "
            "pending_rows=:p, updated_at=:n WHERE id=:v"
        ),
        {"a": accepted, "r": rejected, "w": warning, "p": pending, "n": now(), "v": version_id},
    )
    # A12.2 step 4 reads literally "accepted_rows + warning_rows = 0 ->
    # ALL_REJECTED"; taken byte-for-byte that strands a file whose rows are
    # ALL pending human review (0 accepted, 0 warning, N pending) in
    # REJECTED with no way back — PUBLISH only leaves VALIDATED, and
    # resolving those very rows afterwards (`/price-list-rows/{id}/resolve`,
    # endpoint 90) would have nothing to eventually apply to. Read together
    # with that endpoint's own spec ("PENDING_REVIEW rows resolved later
    # apply immediately if their version is still LIVE"), a pending-only
    # file must be able to reach VALIDATED/SCHEDULED/LIVE once its rows are
    # resolved, so ALL_REJECTED is reserved for when literally nothing —
    # not even something awaiting review — survived (a genuine gap, fixed
    # here per the executor brief).
    return {
        "accepted": accepted, "rejected": rejected, "warning": warning, "pending": pending,
        "all_rejected": (accepted + warning + pending) == 0,
    }
