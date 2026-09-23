"""A12.2 step 3 — vendor column-mapping memory (E-065) and auto-suggestion.

`vendor_column_mapping` is the per-vendor memory described in
مواصفة_استيراد_قائمة_الأسعار.md §2: an ACTIVE mapping keyed by
`(vendor_id, fingerprint)` means "we have seen this exact file shape from
this vendor before" -> the upload is `MAPPING_FOUND`, genuinely zero-click.
A fingerprint never seen before -> `MAPPING_NEEDED`, with every canonical
field pre-filled by the header with the highest label-similarity (threshold
70), for a human to confirm or correct once; that confirmation is what gets
saved back into the memory for every future file in this vendor's own
layout (never re-asked, per §2).

This module's own small string-similarity helper (difflib-based) is
deliberately separate from `rova.matching.match` — that vendored file's
`fuzzy_ratio` is for drug-NAME identity matching (A12.2 step 4, via
`rova.matching.adapter`, the only caller allowed into it); this one compares
short column-header LABELS against the Dicionário, a different problem.
"""
import difflib
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.ids import new_id
from rova.pricelist.fingerprint import normalise_header

# canonical field -> (required, [labels in pt/en/ar worth matching against])
CANONICAL_FIELDS: dict[str, dict] = {
    "vendor_sku": {"required": False, "labels": ["codigo do fornecedor sku", "vendor_sku", "رمز المورد sku"]},
    "product_name_brand": {"required": False, "labels": ["nome comercial", "product_name_brand", "الاسم التجاري"]},
    "product_name_inn": {"required": False, "labels": ["nome cientifico dci", "product_name_inn", "الاسم العلمي dci inn"]},
    "form": {"required": True, "labels": ["forma farmaceutica", "form", "الشكل الصيدلاني"]},
    "strength": {"required": True, "labels": ["dosagem", "strength", "التركيز الجرعة"]},
    "pack_size": {"required": True, "labels": ["unidades por embalagem", "pack_size", "عدد الوحدات في العبوة"]},
    "pack_unit": {"required": False, "labels": ["unidade da embalagem", "pack_unit", "وحدة العبوة"]},
    "manufacturer": {"required": False, "labels": ["fabricante", "manufacturer", "الشركة المصنعة"]},
    "country_of_origin": {"required": False, "labels": ["pais de origem", "country_of_origin", "بلد المنشأ"]},
    "aim_number": {"required": False, "labels": ["numero aim", "aim_number", "رقم التسجيل aim"]},
    "barcode": {"required": False, "labels": ["codigo de barras", "barcode", "الباركود"]},
    "price_to_pharmacy": {"required": True, "labels": ["preco a farmacia mzn", "price_to_pharmacy", "السعر للصيدلية mzn"]},
    "price_to_public": {"required": False, "labels": ["preco ao publico pvp mzn", "price_to_public", "السعر للجمهور pvp mzn"]},
    "discount_percent": {"required": False, "labels": ["desconto do fornecedor", "discount_percent", "نسبة الخصم من المورد"]},
    "cost_to_us": {"required": False, "labels": ["custo confidencial", "cost_to_us", "التكلفة سري"]},
    "stock_status": {"required": False, "labels": ["estado de stock", "stock_status", "حالة المخزون"]},
    "available_quantity": {"required": False, "labels": ["quantidade disponivel", "available_quantity", "الكمية المتاحة"]},
    "batch_number": {"required": False, "labels": ["numero de lote", "batch_number", "رقم الدفعة batch"]},
    "expiry_date": {"required": False, "labels": ["data de validade", "expiry_date", "تاريخ انتهاء الصلاحية"]},
    "min_order_qty": {"required": False, "labels": ["quantidade minima de encomenda", "min_order_qty", "أدنى كمية للطلب"]},
    "currency": {"required": False, "labels": ["moeda", "currency", "العملة"]},
    "effective_from": {"required": False, "labels": ["valido a partir de", "effective_from", "ساري اعتبارا من"]},
    "notes": {"required": False, "labels": ["observacoes", "notes", "ملاحظات"]},
}

# A row must give us either the brand or the INN name (A12.1), plus these three.
REQUIRED_ANY_OF = ("product_name_inn", "product_name_brand")
REQUIRED_ALL_OF = ("pack_size", "price_to_pharmacy")
REQUIRED_ANY_STOCK = ("available_quantity", "stock_status")

# "Also fix while you are in there": this used to be a hard-coded Python
# constant with no §16 key behind it (no spec row names a column-header
# label-similarity threshold — CFG-INGESTION-CONFIDENCE-AUTOMATCH/
# -REVIEW-FLOOR govern §4's row-level *product* matching confidence, a
# different threshold). It is now CFG-PRICELIST-MAPPING-AUTO-SUGGEST-
# THRESHOLD (A2.10: business parameters live in config_parameter, never a
# Python constant), seeded with the same value, 70, it always had — moved,
# not changed. `default=` below is only the seed's own documented fallback
# (A2.10's pattern for every other `cfg.get` call), not a second copy of
# the value.
_AUTO_SUGGEST_THRESHOLD_DEFAULT = 70  # 0-100


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio() * 100


@dataclass(frozen=True)
class MappingSuggestion:
    mapping: dict[str, str | None]  # canonical field -> file header, or None if unmapped
    scores: dict[str, float]


def suggest_mapping(session: Session, headers: list[str]) -> MappingSuggestion:
    threshold = float(cfg.get(
        session, "CFG-PRICELIST-MAPPING-AUTO-SUGGEST-THRESHOLD", default=_AUTO_SUGGEST_THRESHOLD_DEFAULT
    ))
    normalised_headers = {h: normalise_header(h) for h in headers}
    mapping: dict[str, str | None] = {}
    scores: dict[str, float] = {}
    for field, meta in CANONICAL_FIELDS.items():
        best_header, best_score = None, 0.0
        for header, norm_h in normalised_headers.items():
            for label in meta["labels"]:
                score = _ratio(norm_h, label)
                if norm_h == label:
                    score = 100.0
                if score > best_score:
                    best_header, best_score = header, score
        if best_score >= threshold:
            mapping[field] = best_header
            scores[field] = best_score
        else:
            mapping[field] = None
            scores[field] = best_score
    return MappingSuggestion(mapping=mapping, scores=scores)


def validate_mapping_complete(mapping: dict[str, str | None]) -> list[str]:
    """Returns the list of canonical fields still required and missing."""
    missing = []
    if not any(mapping.get(f) for f in REQUIRED_ANY_OF):
        missing.extend(REQUIRED_ANY_OF)
    for f in REQUIRED_ALL_OF:
        if not mapping.get(f):
            missing.append(f)
    if not any(mapping.get(f) for f in REQUIRED_ANY_STOCK):
        missing.extend(REQUIRED_ANY_STOCK)
    return missing


def find_active_mapping(session: Session, *, vendor_id: str, fingerprint: str) -> dict | None:
    row = session.execute(
        text(
            "SELECT * FROM vendor_column_mapping WHERE vendor_id=:v AND fingerprint=:f AND status='ACTIVE'"
        ),
        {"v": vendor_id, "f": fingerprint},
    ).mappings().first()
    return dict(row) if row else None


def save_mapping(
    session: Session, *, vendor_id: str, fingerprint: str, mapping: dict[str, str | None],
    header_row_index: int, created_by_user_id: str, sheet_selector: str | None = None,
) -> dict:
    """R-155: an ACTIVE mapping already at this exact fingerprint is
    replaced in place; any other ACTIVE mapping this vendor has (a different
    fingerprint = a different file shape) is expired, never silently
    reused on a shape it was not confirmed against (§2 E2)."""
    import json as _json

    existing_same = session.execute(
        text("SELECT id FROM vendor_column_mapping WHERE vendor_id=:v AND fingerprint=:f AND status='ACTIVE'"),
        {"v": vendor_id, "f": fingerprint},
    ).scalar()
    session.execute(
        text(
            "UPDATE vendor_column_mapping SET status='EXPIRED', updated_at=:n "
            "WHERE vendor_id=:v AND status='ACTIVE' AND fingerprint <> :f"
        ),
        {"v": vendor_id, "f": fingerprint, "n": now()},
    )
    if existing_same:
        session.execute(
            text(
                "UPDATE vendor_column_mapping SET mapping=CAST(:m AS JSONB), header_row_index=:h, "
                "sheet_selector=:s, last_used_at=:n, updated_at=:n WHERE id=:id"
            ),
            {"m": _json.dumps(mapping), "h": header_row_index, "s": sheet_selector, "n": now(), "id": existing_same},
        )
        return {"id": existing_same}
    mid = new_id("vcm")
    session.execute(
        text(
            "INSERT INTO vendor_column_mapping (id, vendor_id, fingerprint, mapping, sheet_selector, "
            "header_row_index, created_by_user_id) VALUES (:id, :v, :f, CAST(:m AS JSONB), :s, :h, :u)"
        ),
        {"id": mid, "v": vendor_id, "f": fingerprint, "m": _json.dumps(mapping), "s": sheet_selector,
         "h": header_row_index, "u": created_by_user_id},
    )
    return {"id": mid}


def touch_last_used(session: Session, mapping_id: str) -> None:
    session.execute(
        text("UPDATE vendor_column_mapping SET last_used_at=:n WHERE id=:id"), {"n": now(), "id": mapping_id}
    )
