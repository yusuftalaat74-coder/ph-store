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

from rova.core.money import money_str
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


# ── a whole request, priced as it would be paid ──────────────────────────

PRICE_SAME = "SAME"
PRICE_UP = "UP"
PRICE_DOWN = "DOWN"
NO_OFFER = "NO_OFFER"


def _state(seen, now_price):
    if now_price is None:
        return NO_OFFER
    if seen is None or seen == now_price:
        return PRICE_SAME
    return PRICE_UP if now_price > seen else PRICE_DOWN


def price_request(session: Session, request: dict) -> dict:
    """Every line of a request, priced the way checkout will price it.

    This is the cart screen's body and the WhatsApp lane's confirmation
    screen, and it is one function on purpose. The two lanes used to differ
    in the way that matters most: the catalogue lane showed a price and made
    the pharmacist agree to it, while a pasted list went from "confirm" to a
    placed order in one tap with no figure ever on the screen. The shape is
    the same because the question is the same — what will this cost, and has
    anything moved since I looked.

    `price_seen` is whatever the line recorded when it was added; a line that
    never displayed a price has none, and its state is SAME rather than a
    movement invented out of a NULL.
    """
    vendor_names = dict(session.execute(
        text("SELECT id, trade_name FROM vendor_account")).all())

    lines, blocked, by_vendor = [], [], {}
    total = Decimal("0")
    last_touched = request["updated_at"]
    pharmacy_id = request["pharmacy_id"]

    rows = session.execute(
        text("SELECT rl.id, rl.index_product_id, rl.qty_requested, rl.price_seen, "
             "       rl.vendor_seen_id, rl.updated_at, "
             "       p.brand_name, p.inn, p.strength, p.form, p.pack_size, "
             "       p.category, p.image_url, p.regulated_price "
             "FROM request_line rl LEFT JOIN index_product p ON p.id = rl.index_product_id "
             "WHERE rl.request_id=:r AND rl.line_kind='CATALOGUE' "
             "ORDER BY rl.created_at"),
        {"r": request["id"]},
    ).mappings().all()

    for r in rows:
        priced = price_line(session, index_product_id=r["index_product_id"],
                            qty=r["qty_requested"], pharmacy_id=pharmacy_id,
                            strategy=request["allocation_strategy"])
        state = _state(r["price_seen"], priced.unit_price)
        if state == NO_OFFER:
            blocked.append({"line_id": r["id"], "reason": priced.reason or "no fresh offer"})
        else:
            line_total = priced.unit_price * r["qty_requested"]
            total += line_total
            # `None` is a real bucket, not a dropped one: a regulated product
            # is priced from the published reference and has no vendor until
            # checkout ranks it, and silently leaving it out made the
            # per-distributor subtotals stop adding up to the total the
            # pharmacist is about to pay.
            by_vendor[priced.vendor_id] = by_vendor.get(priced.vendor_id, Decimal("0")) + line_total

        if r["updated_at"] and r["updated_at"] > last_touched:
            last_touched = r["updated_at"]

        discount = None
        if priced.list_price and priced.unit_price and priced.list_price > 0 \
                and priced.list_price >= priced.unit_price:
            discount = int(((priced.list_price - priced.unit_price) / priced.list_price) * 100)

        lines.append({
            "id": r["id"], "index_product_id": r["index_product_id"],
            "brand_name": r["brand_name"], "inn": r["inn"],
            "strength": r["strength"], "form": r["form"], "pack_size": r["pack_size"],
            "category": r["category"], "image_url": r["image_url"],
            "qty_requested": r["qty_requested"],
            "price_seen": money_str(r["price_seen"]) if r["price_seen"] is not None else None,
            "price_now": money_str(priced.unit_price) if priced.unit_price is not None else None,
            "price_state": state,
            "vendor_id": priced.vendor_id,
            "vendor_name": vendor_names.get(priced.vendor_id) if priced.vendor_id else None,
            "list_price": money_str(priced.list_price) if priced.list_price is not None else None,
            "discount_pct": discount,
            "regulated_price": bool(r["regulated_price"]),
        })


    return {
        "lines": lines,
        "last_touched_at": last_touched,
        "totals": {
            "goods_total": money_str(total),
            "by_vendor": [{"vendor_id": v,
                           "vendor_name": vendor_names.get(v) if v else None,
                           "goods_total": money_str(a)}
                          for v, a in sorted(by_vendor.items(), key=lambda kv: kv[0] or "")],
        },
        "checkout_blocked_by": blocked,
    }
