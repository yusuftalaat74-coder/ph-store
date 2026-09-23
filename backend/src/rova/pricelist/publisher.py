"""A12.2 step 6 (`publisher.py`, R-156) — applies a version's ACCEPTED/
WARNING rows to `vendor_offer`, versioned per §5 of
مواصفة_استيراد_قائمة_الأسعار.md: a matching live offer is UPDATED in place
(price/qty/pack/expiry refreshed, `stock_confirmed_at` reset), never
duplicated; a product absent from a non-`partial_update` version is
withdrawn (never deleted, R-060) through the real SM-12 machine; a
previously-`WITHDRAWN` offer is never revived — a fresh row is created
instead, matching the rule already in force for `PriceReference.superseded_by`.
"""
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.core.clock import now
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.money import quantize
from rova.domain.machines.registry import MACHINES


def _net_free_price(price_to_pharmacy: Decimal, discount_pct: Decimal | None) -> Decimal:
    discount = discount_pct or Decimal("0")
    return quantize(price_to_pharmacy * (Decimal("1") - discount))


def go_live(session: Session, version_id: str) -> dict:
    version = session.execute(
        text("SELECT * FROM price_list_version WHERE id=:v"), {"v": version_id}
    ).mappings().one()
    vendor_id = version["vendor_id"]
    effective_from = version["effective_from"] or now()

    rows = session.execute(
        text(
            "SELECT * FROM price_list_row WHERE version_id=:v AND outcome IN ('ACCEPTED','WARNING') "
            "AND index_product_id IS NOT NULL ORDER BY row_index"
        ),
        {"v": version_id},
    ).mappings().all()

    covered_products: set[str] = set()
    created = updated = 0
    for row in rows:
        product = session.execute(
            text("SELECT regulated_price, pack_size FROM index_product WHERE id=:p"),
            {"p": row["index_product_id"]},
        ).mappings().one()
        covered_products.add(row["index_product_id"])

        price = None
        if not product["regulated_price"]:
            price = _net_free_price(row["price_to_pharmacy"], row["discount_pct"])

        existing = session.execute(
            text(
                "SELECT id FROM vendor_offer WHERE vendor_id=:v AND index_product_id=:p "
                "AND freshness_state <> 'WITHDRAWN'"
            ),
            {"v": vendor_id, "p": row["index_product_id"]},
        ).mappings().first()

        if existing:
            session.execute(
                text(
                    "UPDATE vendor_offer SET qty_available=:qty, pack_size=:pack, "
                    "expiry_horizon_days=:exp, min_order_qty=:moq, price=:price, "
                    "stock_confirmed_at=:sc, freshness_state='FRESH', source_row_id=:src, updated_at=:n "
                    "WHERE id=:id"
                ),
                {"qty": row["qty_available"], "pack": row["pack_size"] or product["pack_size"],
                 "exp": row["expiry_horizon_days"], "moq": row["min_order_qty"], "price": price,
                 "sc": effective_from, "src": row["id"], "n": now(), "id": existing["id"]},
            )
            offer_id = existing["id"]
            updated += 1
        else:
            offer_id = new_id("ofr")
            session.execute(
                text(
                    "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
                    "pack_size, min_order_qty, expiry_horizon_days, price, stock_confirmed_at, freshness_state, "
                    "source_row_id) VALUES (:id, :v, :p, :reg, :qty, :pack, :moq, :exp, :price, :sc, 'FRESH', :src)"
                ),
                {"id": offer_id, "v": vendor_id, "p": row["index_product_id"], "reg": product["regulated_price"],
                 "qty": row["qty_available"], "pack": row["pack_size"] or product["pack_size"],
                 "moq": row["min_order_qty"], "exp": row["expiry_horizon_days"], "price": price,
                 "sc": effective_from, "src": row["id"]},
            )
            created += 1

        session.execute(text("UPDATE price_list_row SET resulting_offer_id=:o WHERE id=:id"),
                         {"o": offer_id, "id": row["id"]})

    withdrawn = 0
    if not version["partial_update"]:
        live_offers = session.execute(
            text(
                "SELECT id, index_product_id FROM vendor_offer WHERE vendor_id=:v "
                "AND freshness_state <> 'WITHDRAWN'"
            ),
            {"v": vendor_id},
        ).mappings().all()
        for offer in live_offers:
            if offer["index_product_id"] not in covered_products:
                MACHINES["SM-12"].apply(session, offer["id"], "WITHDRAW", "SYSTEM")
                withdrawn += 1

    previous_live = session.execute(
        text("SELECT id FROM price_list_version WHERE vendor_id=:v AND status='LIVE' AND id <> :self"),
        {"v": vendor_id, "self": version_id},
    ).scalar()
    if previous_live:
        MACHINES["SM-21"].apply(session, previous_live, "SUPERSEDE", "SYSTEM")
        session.execute(text("UPDATE price_list_version SET supersedes_id=:p WHERE id=:v"),
                         {"p": previous_live, "v": version_id})

    session.execute(
        text("INSERT INTO audit_event (id, actor_user_id, actor_role, action_code, subject_type, subject_id, "
             "rule_ref) VALUES (:id, NULL, 'SYSTEM', 'PRICELIST_PUBLISHED', 'price_list_version', :v, 'R-156')"),
        {"id": new_id("aud"), "v": version_id},
    )
    return {"created": created, "updated": updated, "withdrawn": withdrawn}


def resolve_pending_row(session: Session, row_id: str, *, index_product_id: str | None) -> dict:
    """A12.2 step 6 last sentence — a PENDING_REVIEW row resolved by a human
    applies immediately if its version is still LIVE, otherwise is marked
    ignored in `outcome_detail`."""
    row = session.execute(text("SELECT * FROM price_list_row WHERE id=:id"), {"id": row_id}).mappings().one()
    version = session.execute(
        text("SELECT * FROM price_list_version WHERE id=:v"), {"v": row["version_id"]}
    ).mappings().one()

    if index_product_id is None:
        # §7 case 2 of مواصفة_استيراد_قائمة_الأسعار.md: confirmed as genuinely
        # absent from the Index -> handed to IndexPharmacist to create it
        # under its own governance; no offer is published either way. No
        # `demand_gap` row here (that table is keyed to a requesting
        # pharmacy, A3 §4 — a price-list row has no pharmacy context).
        session.execute(
            text(
                "UPDATE price_list_row SET outcome='REJECTED', outcome_reason='UNMATCHED_PRODUCT', "
                "outcome_detail='confirmado pelo revisor: produto ainda não existe no Índice — encaminhado ao "
                "IndexPharmacist', resolved_at=:n, updated_at=:n WHERE id=:id"
            ),
            {"n": now(), "id": row_id},
        )
        return {"outcome": "REJECTED", "applied": False}

    product = session.execute(
        text("SELECT * FROM index_product WHERE id=:p"), {"p": index_product_id}
    ).mappings().first()
    if product is None:
        raise ApiError("NOT_FOUND", "index product not found")

    session.execute(
        text(
            "UPDATE price_list_row SET index_product_id=:p, outcome='ACCEPTED', outcome_reason=NULL, "
            "outcome_detail='resolvido manualmente por revisor', resolved_at=:n, updated_at=:n WHERE id=:id"
        ),
        {"p": index_product_id, "n": now(), "id": row_id},
    )
    session.execute(
        text("UPDATE price_list_version SET pending_rows = GREATEST(pending_rows - 1, 0), "
             "accepted_rows = accepted_rows + 1, updated_at=:n WHERE id=:v"),
        {"n": now(), "v": version["id"]},
    )

    if version["status"] == "LIVE":
        row = session.execute(text("SELECT * FROM price_list_row WHERE id=:id"), {"id": row_id}).mappings().one()
        _apply_single_row(session, version, row, product)
        return {"outcome": "ACCEPTED", "applied": True}
    return {"outcome": "ACCEPTED", "applied": False}


def _apply_single_row(session: Session, version: dict, row: dict, product: dict) -> None:
    price = None
    if not product["regulated_price"]:
        price = _net_free_price(row["price_to_pharmacy"], row["discount_pct"])
    existing = session.execute(
        text("SELECT id FROM vendor_offer WHERE vendor_id=:v AND index_product_id=:p AND freshness_state <> 'WITHDRAWN'"),
        {"v": version["vendor_id"], "p": product["id"]},
    ).mappings().first()
    if existing:
        session.execute(
            text("UPDATE vendor_offer SET qty_available=:qty, pack_size=:pack, expiry_horizon_days=:exp, "
                 "price=:price, stock_confirmed_at=:n, freshness_state='FRESH', source_row_id=:src, updated_at=:n "
                 "WHERE id=:id"),
            {"qty": row["qty_available"], "pack": row["pack_size"] or product["pack_size"],
             "exp": row["expiry_horizon_days"], "price": price, "n": now(), "src": row["id"], "id": existing["id"]},
        )
        offer_id = existing["id"]
    else:
        offer_id = new_id("ofr")
        session.execute(
            text("INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
                 "pack_size, expiry_horizon_days, price, stock_confirmed_at, freshness_state, source_row_id) "
                 "VALUES (:id, :v, :p, :reg, :qty, :pack, :exp, :price, :n, 'FRESH', :src)"),
            {"id": offer_id, "v": version["vendor_id"], "p": product["id"], "reg": product["regulated_price"],
             "qty": row["qty_available"], "pack": row["pack_size"] or product["pack_size"],
             "exp": row["expiry_horizon_days"], "price": price, "n": now(), "src": row["id"]},
        )
    session.execute(text("UPDATE price_list_row SET resulting_offer_id=:o WHERE id=:id"),
                     {"o": offer_id, "id": row["id"]})
