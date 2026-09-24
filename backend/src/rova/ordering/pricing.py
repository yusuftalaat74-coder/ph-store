"""What one request line costs, decided once and used everywhere.

The cart and `checkout()` have to agree to the metical. They did not before
this module existed: the storefront showed `best_price` — the cheapest fresh
offer in the catalogue — while `checkout()` priced each line from the top
candidate the ranking engine returns under the request's allocation
strategy, which is fee-blind but not always the cheapest. A pharmacist who
adds a 331,00 line and is charged 440,00 has been lied to, however
defensible each number is on its own.

So both call `price_line`. A price that appears on a screen and a price that
appears on an invoice now come from the same function.

A regulated product is priced from `price_reference`, never from a vendor
offer — R-009 makes a vendor price on a regulated product unrepresentable in
the first place, and this keeps the two consistent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.ranking.engine import rank
from rova.ranking.inputs import build_ranking_input


@dataclass(frozen=True)
class LinePrice:
    """`unit_price is None` means nothing can be sold today: either no fresh
    offer, or a regulated product with no published reference price. The
    caller decides what to do about it — the cart marks the line, checkout
    refuses. Neither invents a number."""
    unit_price: Decimal | None
    vendor_id: str | None
    offer_id: str | None
    list_price: Decimal | None
    regulated: bool
    reason: str | None = None          # why there is no price


def price_line(session: Session, *, index_product_id: str | None, qty: int,
               pharmacy_id: str, strategy: str) -> LinePrice:
    if not index_product_id:
        return LinePrice(None, None, None, None, False, "line has no product yet")

    product = session.execute(
        text("SELECT regulated_price FROM index_product WHERE id=:p"),
        {"p": index_product_id},
    ).mappings().first()
    if product is None:
        return LinePrice(None, None, None, None, False, "product not found")

    if product["regulated_price"]:
        ref = session.execute(
            text("SELECT id, wholesale_derived_price FROM price_reference "
                 "WHERE index_product_id=:p AND effective_from <= :today "
                 "ORDER BY effective_from DESC LIMIT 1"),
            {"p": index_product_id, "today": date.today()},
        ).mappings().first()
        if ref is None:
            # R-006: a regulated product with no reference cannot be sold.
            return LinePrice(None, None, None, None, True, "no price reference")
        return LinePrice(ref["wholesale_derived_price"], None, None, None, True)

    result = rank(build_ranking_input(
        session, index_product_id=index_product_id, qty_requested=qty,
        pharmacy_id=pharmacy_id, strategy=strategy,
    ))
    if not result.ordered:
        return LinePrice(None, None, None, None, False, "no fresh offer")

    top = result.ordered[0]
    listed = session.execute(
        text("SELECT list_price FROM vendor_offer WHERE id=:o"), {"o": top.offer_id},
    ).scalar()
    return LinePrice(top.price, top.vendor_id, top.offer_id, listed, False)
