"""The cart: a `DRAFT` request, read back priced as it will actually be paid.

The cart used to be a JavaScript array. Closing the app lost it. Making it a
`DRAFT` request costs no new table and no new entity — `SM-01` already has
the state, its guards already hold, and the move out of it is already
recorded in the append-only `state_transition` log.

Two rules this module exists to keep:

* **The price on the screen is the price on the invoice.** Every read
  reprices through `price_line`, the same function `checkout()` uses.
* **Nothing reprices silently.** `price_seen` records what the pharmacist
  was shown when the line went in, and only their own acknowledgement moves
  it. A cart whose numbers change while nobody is looking is worse than one
  that says so.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.money import money_str
from rova.ordering.pricing import price_line

# The cart is one particular kind of DRAFT: built in the app from the
# catalogue. A DRAFT that came back from `ADMIN_REJECT`, or one built from a
# WhatsApp paste, is a different thing and is left alone.
CART_MODE = "CATALOGUE"
CART_CHANNEL = "APP"

PRICE_SAME = "SAME"
PRICE_UP = "UP"
PRICE_DOWN = "DOWN"
NO_OFFER = "NO_OFFER"


def find_cart(session: Session, pharmacy_id: str) -> dict | None:
    """The pharmacy's open cart, newest first.

    Newest rather than only: `ADMIN_REJECT` returns a request from
    `AWAITING_ADMIN_APPROVAL` to `DRAFT`, so a second cart can legitimately
    exist for a moment. A unique index would have failed that transition; the
    rule belongs here instead, where it can prefer one without forbidding the
    other.
    """
    return session.execute(
        text("SELECT * FROM request WHERE pharmacy_id=:p AND status='DRAFT' "
             "AND mode=:m AND channel=:c ORDER BY created_at DESC LIMIT 1"),
        {"p": pharmacy_id, "m": CART_MODE, "c": CART_CHANNEL},
    ).mappings().first()


def _open_cart(session: Session, *, pharmacy_id: str, user_id: str,
               strategy: str = "FEWEST_VENDORS") -> dict:
    existing = find_cart(session, pharmacy_id)
    if existing is not None:
        return dict(existing)

    year = session.execute(text("SELECT EXTRACT(YEAR FROM now())::int")).scalar()
    seq = session.execute(text("SELECT nextval('request_number_seq')")).scalar()
    rid = new_id("req")
    session.execute(
        text("INSERT INTO request (id, number, pharmacy_id, mode, channel, status, "
             "allocation_strategy, created_by_user_id) "
             "VALUES (:id, :num, :ph, :m, :c, 'DRAFT', :s, :u)"),
        {"id": rid, "num": f"RQ-{year}-{str(seq).zfill(6)}", "ph": pharmacy_id,
         "m": CART_MODE, "c": CART_CHANNEL, "s": strategy, "u": user_id},
    )
    return dict(session.execute(
        text("SELECT * FROM request WHERE id=:r"), {"r": rid}).mappings().one())


def touch(session: Session, request_id: str) -> None:
    """`last_touched_at` is what the idle-cart job reads, and adding a line
    does not otherwise change the request row at all."""
    session.execute(text("UPDATE request SET updated_at = now() WHERE id=:r"),
                    {"r": request_id})


def set_line(session: Session, *, pharmacy_id: str, user_id: str,
             index_product_id: str, qty: int) -> str:
    """Set a product's quantity in the cart — set, not add.

    The phone sends the number it is displaying, so a retry after a dropped
    connection lands on the same quantity instead of doubling it. Merging is
    limited to `CATALOGUE` lines: two lines of a WhatsApp paste may resolve to
    the same product and both deserve to exist.
    """
    if qty < 1:
        raise ApiError("VALIDATION_ERROR", "qty_requested must be at least 1")

    cart = _open_cart(session, pharmacy_id=pharmacy_id, user_id=user_id)
    existing = session.execute(
        text("SELECT id FROM request_line WHERE request_id=:r AND index_product_id=:p "
             "AND line_kind='CATALOGUE' LIMIT 1"),
        {"r": cart["id"], "p": index_product_id},
    ).scalar()

    priced = price_line(session, index_product_id=index_product_id, qty=qty,
                        pharmacy_id=pharmacy_id, strategy=cart["allocation_strategy"])

    if existing:
        session.execute(
            text("UPDATE request_line SET qty_requested=:q, updated_at=now() WHERE id=:id"),
            {"q": qty, "id": existing},
        )
        line_id = existing
    else:
        line_id = new_id("rql")
        session.execute(
            text("INSERT INTO request_line (id, request_id, index_product_id, qty_requested, "
                 "line_kind, match_status, price_seen, vendor_seen_id) "
                 "VALUES (:id, :r, :p, :q, 'CATALOGUE', 'RESOLVED', :ps, :vs)"),
            {"id": line_id, "r": cart["id"], "p": index_product_id, "q": qty,
             "ps": priced.unit_price, "vs": priced.vendor_id},
        )
    touch(session, cart["id"])
    return cart["id"]


def set_qty(session: Session, *, line_id: str, pharmacy_id: str, qty: int) -> str:
    row = session.execute(
        text("SELECT rl.request_id, r.status, r.pharmacy_id FROM request_line rl "
             "JOIN request r ON r.id = rl.request_id WHERE rl.id=:id"),
        {"id": line_id},
    ).mappings().first()
    if row is None or row["pharmacy_id"] != pharmacy_id:
        raise ApiError("NOT_FOUND", "request line not found")
    if row["status"] != "DRAFT":
        raise ApiError("GUARD_FAILED",
                       f"the request is {row['status']}; lines change only in DRAFT",
                       rule="SM-01")
    if qty < 1:
        raise ApiError("VALIDATION_ERROR", "qty_requested must be at least 1")
    session.execute(
        text("UPDATE request_line SET qty_requested=:q, updated_at=now() WHERE id=:id"),
        {"q": qty, "id": line_id})
    touch(session, row["request_id"])
    return row["request_id"]


def _state(seen: Decimal | None, now_price: Decimal | None) -> str:
    if now_price is None:
        return NO_OFFER
    if seen is None or seen == now_price:
        return PRICE_SAME
    return PRICE_UP if now_price > seen else PRICE_DOWN


def read_cart(session: Session, pharmacy_id: str) -> dict:
    """The whole cart, priced now. Every write returns this too, so the phone
    replaces what it holds instead of merging — two devices editing the same
    cart then converge on the next tap rather than drifting."""
    cart = find_cart(session, pharmacy_id)
    if cart is None:
        return {"request_id": None, "number": None, "status": None,
                "last_touched_at": None, "lines": [],
                "totals": {"goods_total": "0.00", "by_vendor": []},
                "checkout_blocked_by": []}

    rows = session.execute(
        text("SELECT rl.id, rl.index_product_id, rl.qty_requested, rl.price_seen, "
             "       rl.vendor_seen_id, rl.updated_at, "
             "       p.brand_name, p.inn, p.strength, p.form, p.pack_size, "
             "       p.category, p.image_url, p.regulated_price "
             "FROM request_line rl LEFT JOIN index_product p ON p.id = rl.index_product_id "
             "WHERE rl.request_id=:r AND rl.line_kind='CATALOGUE' "
             "ORDER BY rl.created_at"),
        {"r": cart["id"]},
    ).mappings().all()

    vendor_names = dict(session.execute(
        text("SELECT id, trade_name FROM vendor_account")).all())

    lines, blocked, by_vendor = [], [], {}
    total = Decimal("0")
    last_touched = cart["updated_at"]

    for r in rows:
        priced = price_line(session, index_product_id=r["index_product_id"],
                            qty=r["qty_requested"], pharmacy_id=pharmacy_id,
                            strategy=cart["allocation_strategy"])
        state = _state(r["price_seen"], priced.unit_price)
        if state == NO_OFFER:
            blocked.append({"line_id": r["id"], "reason": priced.reason or "no fresh offer"})
        else:
            line_total = priced.unit_price * r["qty_requested"]
            total += line_total
            if priced.vendor_id:
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
        "request_id": cart["id"], "number": cart["number"], "status": cart["status"],
        "last_touched_at": last_touched.isoformat() if last_touched else None,
        "lines": lines,
        "totals": {
            "goods_total": money_str(total),
            "by_vendor": [{"vendor_id": v, "vendor_name": vendor_names.get(v, v),
                           "goods_total": money_str(a)} for v, a in sorted(by_vendor.items())],
        },
        "checkout_blocked_by": blocked,
    }
