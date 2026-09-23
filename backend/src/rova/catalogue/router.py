"""A15.4 rows 58-60, A6.4 search (reduced: plain normalised LIKE, no fuzzy
matcher fallback in this session — see manifest)."""
import re
import unicodedata

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.domain.enums import RoleCode
from rova.ranking.engine import rank
from rova.ranking.inputs import build_ranking_input

router = APIRouter(prefix="/v1/catalogue", tags=["catalogue"])

_READERS = (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER, RoleCode.PHARMACY_RECEIVER, RoleCode.OPS_REVIEWER)


def _normalise(q: str) -> str:
    q = unicodedata.normalize("NFKD", q).encode("ascii", "ignore").decode()
    q = q.lower().strip()
    return re.sub(r"\s+", " ", q)


@router.get("/search")
def search(q: str, principal: Principal = Depends(require_roles(*_READERS)), session: Session = Depends(get_session, scope="function")):
    needle = f"%{_normalise(q)}%"
    rows = session.execute(
        text(
            "SELECT id, inn, brand_name, form, strength, pack_size, regulated_price, "
            "(SELECT count(*) FROM vendor_offer o WHERE o.index_product_id = index_product.id AND o.freshness_state='FRESH') AS fresh_offer_count "
            "FROM index_product WHERE review_status='PUBLISHED' AND search_text LIKE :q LIMIT 50"
        ),
        {"q": needle},
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "next_cursor": None}


@router.get("/products/{product_id}")
def get_product(product_id: str, principal: Principal = Depends(require_roles(*_READERS)),
                 session: Session = Depends(get_session, scope="function")):
    product = session.execute(
        text("SELECT * FROM index_product WHERE id=:p AND review_status='PUBLISHED'"), {"p": product_id}
    ).mappings().first()
    if product is None:
        raise ApiError("NOT_FOUND", "product not found")

    result = rank(build_ranking_input(
        session, index_product_id=product_id, qty_requested=1,
        pharmacy_id=principal.pharmacy_id or "", strategy="FEWEST_VENDORS",
    ))
    offers = [
        {"vendor_id": c.vendor_id, "offer_id": c.offer_id, "price": str(c.price) if c.price is not None else None,
         "qty_available": c.qty_available, "expiry_horizon_days": c.expiry_horizon_days}
        for c in result.ordered
    ]
    text_pt = None
    if result.single_candidate:
        text_pt = "Único fornecedor disponível de momento para este produto — mais fornecedores a aderir."

    price_reference = None
    if product["regulated_price"]:
        ref = session.execute(
            text("SELECT pvp_price, wholesale_derived_price FROM price_reference WHERE index_product_id=:p "
                 "ORDER BY effective_from DESC LIMIT 1"),
            {"p": product_id},
        ).mappings().first()
        if ref:
            price_reference = {"pvp_price": str(ref["pvp_price"]), "wholesale_derived_price": str(ref["wholesale_derived_price"])}

    return {**dict(product), "offers": offers, "single_candidate": result.single_candidate,
            "single_candidate_text_pt": text_pt, "price_reference": price_reference}
