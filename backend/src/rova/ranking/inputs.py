"""A6.1 — the ONLY module allowed to build a RankingInput from the database.
Allowed tables, and nothing else: vendor_offer, vendor_account, vendor_score,
credit_facility, "order", index_product, price_reference. This module must
never mention, even in a comment, any commercial-terms or price-list table —
tests/domain/test_ranking_blind.py greps this file's source for that closed
list (see FORBIDDEN_STRINGS there) and fails the whole ranking-blindness
guarantee if one appears."""
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.ranking.engine import Candidate, RankingInput


def build_ranking_input(
    session: Session, *, index_product_id: str, qty_requested: int, pharmacy_id: str,
    strategy: str, excluded_vendor_ids: frozenset[str] = frozenset(),
) -> RankingInput:
    product = session.execute(
        text("SELECT regulated_price FROM index_product WHERE id=:p"), {"p": index_product_id}
    ).mappings().one()

    offers = session.execute(
        text(
            "SELECT o.id AS offer_id, o.vendor_id, o.freshness_state, o.qty_available, o.min_order_qty, "
            "o.expiry_horizon_days, o.price, v.status AS vendor_status, v.mov_amount, v.delivery_mode "
            "FROM vendor_offer o JOIN vendor_account v ON v.id = o.vendor_id "
            "WHERE o.index_product_id=:p"
        ),
        {"p": index_product_id},
    ).mappings().all()

    candidates = []
    for row in offers:
        vendor_id = row["vendor_id"]
        score_row = session.execute(
            text("SELECT fill_rate, on_time_dispatch_rate, promise_accuracy, score_value FROM vendor_score "
                 "WHERE vendor_id=:v ORDER BY computed_at DESC LIMIT 1"),
            {"v": vendor_id},
        ).mappings().first()
        fill_rate = score_row["fill_rate"] if score_row else Decimal("1.0000")
        on_time = score_row["on_time_dispatch_rate"] if score_row else Decimal("1.0000")
        promise_acc = score_row["promise_accuracy"] if score_row else Decimal("1.0000")
        score_value = score_row["score_value"] if score_row else Decimal("100.00")

        median_hours = session.execute(
            text(
                "SELECT EXTRACT(EPOCH FROM percentile_cont(0.5) WITHIN GROUP ("
                "  ORDER BY dispatched_at - accepted_at)) / 3600.0 "
                'FROM "order" WHERE vendor_id=:v AND dispatched_at IS NOT NULL AND accepted_at IS NOT NULL'
            ),
            {"v": vendor_id},
        ).scalar()

        facility = session.execute(
            text("SELECT terms_days, status FROM credit_facility WHERE vendor_id=:v AND pharmacy_id=:ph"),
            {"v": vendor_id, "ph": pharmacy_id},
        ).mappings().first()
        terms_available = ["UPFRONT"]
        credit_days = None
        if facility and facility["status"] == "ACTIVE" and facility["terms_days"]:
            terms_available.append("CREDIT_N_DAYS")
            credit_days = facility["terms_days"]
        if row["delivery_mode"] != "PLATFORM_COORDINATED_COURIER":
            terms_available.append("COD")

        goods_value_estimate = (row["price"] or Decimal("0")) * qty_requested
        mov_reached = goods_value_estimate >= row["mov_amount"] if row["mov_amount"] else True

        candidates.append(
            Candidate(
                vendor_id=vendor_id,
                offer_id=row["offer_id"],
                freshness_state=row["freshness_state"],
                qty_available=row["qty_available"],
                min_order_qty=row["min_order_qty"],
                expiry_horizon_days=row["expiry_horizon_days"],
                price=row["price"],
                fill_rate=fill_rate,
                on_time_dispatch_rate=on_time,
                promise_accuracy=promise_acc,
                score_value=score_value,
                median_promised_dispatch_hours=Decimal(str(median_hours)) if median_hours is not None else None,
                payment_terms_available=tuple(terms_available),
                credit_days=credit_days,
                mov_amount=row["mov_amount"],
                mov_reached=mov_reached,
                vendor_status=row["vendor_status"],
            )
        )

    return RankingInput(
        index_product_id=index_product_id,
        regulated_price=product["regulated_price"],
        qty_requested=qty_requested,
        pharmacy_id=pharmacy_id,
        strategy=strategy,
        candidates=tuple(candidates),
        excluded_vendor_ids=excluded_vendor_ids,
    )
