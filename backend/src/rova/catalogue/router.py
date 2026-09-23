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


SHELF_KEY = "coalesce(nullif(p.manufacturer,''), nullif(p.therapeutic_class,''), nullif(p.form,''), 'Outros')"

_LIST_COLUMNS = (
    "p.id, p.inn, p.brand_name, p.form, p.strength, p.pack_size, "
    "p.therapeutic_class, p.regulated_price, "
    "(SELECT count(*) FROM vendor_offer o WHERE o.index_product_id = p.id "
    " AND o.freshness_state='FRESH') AS fresh_offer_count, "
    "(SELECT min(o.price) FROM vendor_offer o WHERE o.index_product_id = p.id "
    " AND o.freshness_state='FRESH' AND o.price IS NOT NULL) AS best_price"
)


@router.get("/search")
def search(q: str, offset: int = 0, limit: int = 50,
           in_stock: bool = False,
           principal: Principal = Depends(require_roles(*_READERS)),
           session: Session = Depends(get_session, scope="function")):
    """A shelf is useless without a price, so each row carries the cheapest
    fresh offer and how many vendors have it. `best_price` is a string like
    every other money value in this API — never a JSON number."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    needle = f"%{_normalise(q)}%"
    having = ("AND EXISTS (SELECT 1 FROM vendor_offer o WHERE o.index_product_id = p.id "
              "AND o.freshness_state='FRESH')") if in_stock else ""
    rows = session.execute(
        text(
            # PostgreSQL will not accept a select alias inside an ORDER BY
            # expression — only a bare alias — so the ordering happens one
            # level out. Writing `ORDER BY (best_price IS NULL)` against the
            # inner select raises `UndefinedColumn`, which the API would
            # return as a bare 500.
            "SELECT * FROM ("
            f"  SELECT {_LIST_COLUMNS} FROM index_product p "
            f"  WHERE p.review_status='PUBLISHED' AND p.search_text LIKE :q {having}"
            ") x ORDER BY (x.best_price IS NULL), x.brand_name "
            "LIMIT :limit OFFSET :offset"
        ),
        {"q": needle, "limit": limit + 1, "offset": offset},
    ).mappings().all()
    more = len(rows) > limit
    return {"items": [_row(r) for r in rows[:limit]],
            "next_offset": offset + limit if more else None,
            "next_cursor": None}


def _row(r) -> dict:
    d = dict(r)
    if d.get("best_price") is not None:
        d["best_price"] = str(d["best_price"])
    return d


@router.get("/shelves")
def shelves(principal: Principal = Depends(require_roles(*_READERS)),
            session: Session = Depends(get_session, scope="function")):
    """What the store opens on: the categories that actually have something
    to sell, biggest first. Only products with a fresh offer are counted —
    a shelf the pharmacist cannot order from is not worth a tap."""
    rows = session.execute(text(
        # A pharmacy buyer browses by the house whose products they stock —
        # Bluepharma, BIAL, Adcock Ingram — not by dosage form, which is what
        # grouping on `form` produced: a "Comprimidos" shelf holding half the
        # catalogue tells nobody anything. Therapeutic class and form remain
        # as fallbacks for rows the registers did not give a maker for.
        f"SELECT {SHELF_KEY} AS shelf, count(*) AS product_count "
        "FROM index_product p "
        "WHERE p.review_status='PUBLISHED' AND p.manufacturer <> '—' "
        "  AND EXISTS (SELECT 1 FROM vendor_offer o "
        "              WHERE o.index_product_id = p.id AND o.freshness_state='FRESH') "
        "GROUP BY 1 HAVING count(*) > 0 ORDER BY 2 DESC, 1 LIMIT 40"
    )).mappings().all()
    total = session.execute(text(
        "SELECT count(*) FROM index_product WHERE review_status='PUBLISHED'")).scalar()
    offered = session.execute(text(
        "SELECT count(DISTINCT index_product_id) FROM vendor_offer "
        "WHERE freshness_state='FRESH'")).scalar()
    return {"items": [dict(r) for r in rows],
            "catalogue_size": total, "products_with_offers": offered}


@router.get("/shelf")
def shelf(name: str, offset: int = 0, limit: int = 50,
          principal: Principal = Depends(require_roles(*_READERS)),
          session: Session = Depends(get_session, scope="function")):
    """One shelf's products. Same row shape as search, so the store renders
    a browse result and a search result with one piece of code."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    rows = session.execute(
        text(
            "SELECT * FROM ("
            f"  SELECT {_LIST_COLUMNS} FROM index_product p "
            "  WHERE p.review_status='PUBLISHED' "
            f"    AND {SHELF_KEY} = :name "
            "    AND EXISTS (SELECT 1 FROM vendor_offer o WHERE o.index_product_id = p.id "
            "                AND o.freshness_state='FRESH')"
            ") x ORDER BY (x.best_price IS NULL), x.brand_name "
            "LIMIT :limit OFFSET :offset"
        ),
        {"name": name, "limit": limit + 1, "offset": offset},
    ).mappings().all()
    more = len(rows) > limit
    return {"shelf": name, "items": [_row(r) for r in rows[:limit]],
            "next_offset": offset + limit if more else None}


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
    names = dict(session.execute(text(
        "SELECT id, trade_name FROM vendor_account")).all())
    offers = [
        {"vendor_id": c.vendor_id, "vendor_name": names.get(c.vendor_id, c.vendor_id),
         "offer_id": c.offer_id, "price": str(c.price) if c.price is not None else None,
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
