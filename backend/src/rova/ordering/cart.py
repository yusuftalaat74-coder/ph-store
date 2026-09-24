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

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.money import money_str
from rova.ordering.pricing import NO_OFFER, PRICE_SAME, price_line, price_request

# The cart is a DRAFT that says so. `is_cart` is set only here, by the cart
# endpoints, and `uq_one_cart_per_pharmacy` makes it unique per pharmacy
# while the request is still DRAFT. The mode and channel below describe what
# it is -- built in the app from the catalogue -- but they no longer identify
# it: a request made through `POST /v1/requests` carries exactly the same
# two values and is not a cart.
CART_MODE = "CATALOGUE"
CART_CHANNEL = "APP"

def find_cart(session: Session, pharmacy_id: str) -> dict | None:
    """The pharmacy's one open cart.

    `uq_one_cart_per_pharmacy` (migration 0006) makes a second one
    impossible, so this is a lookup and not a choice. It used to be a choice
    -- newest of the DRAFT/CATALOGUE/APP requests -- and that was the bug:
    two taps on `+` before the first reply landed created two, and the one
    the pharmacist could not see was still counted in his order list.
    """
    return session.execute(
        text("SELECT * FROM request WHERE pharmacy_id=:p AND status='DRAFT' AND is_cart"),
        {"p": pharmacy_id},
    ).mappings().first()


def _open_cart(session: Session, *, pharmacy_id: str, user_id: str,
               strategy: str = "FEWEST_VENDORS") -> dict:
    existing = find_cart(session, pharmacy_id)
    if existing is not None:
        return dict(existing)

    year = session.execute(text("SELECT EXTRACT(YEAR FROM now())::int")).scalar()
    seq = session.execute(text("SELECT nextval('request_number_seq')")).scalar()
    rid = new_id("req")
    try:
        # A SAVEPOINT, so losing the race costs this statement and not the
        # caller's whole transaction.
        with session.begin_nested():
            session.execute(
                text("INSERT INTO request (id, number, pharmacy_id, mode, channel, status, "
                     "allocation_strategy, created_by_user_id, is_cart) "
                     "VALUES (:id, :num, :ph, :m, :c, 'DRAFT', :s, :u, true)"),
                {"id": rid, "num": f"RQ-{year}-{str(seq).zfill(6)}", "ph": pharmacy_id,
                 "m": CART_MODE, "c": CART_CHANNEL, "s": strategy, "u": user_id},
            )
    except IntegrityError:
        # `uq_one_cart_per_pharmacy` fired: two taps on `+` arrived together
        # and the other one won. Its cart is the cart — the pharmacist tapped
        # twice and expects one basket, not an error.
        existing = find_cart(session, pharmacy_id)
        if existing is None:
            raise
        return dict(existing)
    return dict(session.execute(
        text("SELECT * FROM request WHERE id=:r"), {"r": rid}).mappings().one())


def touch(session: Session, request_id: str) -> None:
    """`request.updated_at` is what the cart reports as `last_touched_at` and
    what the idle-cart job will read; adding a line does not otherwise change
    the request row at all.

    Edits touch, reads do not — so this measures when the basket last
    changed, not when it was last looked at."""
    session.execute(text("UPDATE request SET updated_at = now() WHERE id=:r"),
                    {"r": request_id})


def set_line(session: Session, *, pharmacy_id: str, user_id: str,
             index_product_id: str, qty: int,
             price_shown: Decimal | None = None) -> str:
    """Set a product's quantity in the cart — set, not add.

    The phone sends the number it is displaying, so a retry after a dropped
    connection lands on the same quantity instead of doubling it. Merging is
    limited to `CATALOGUE` lines: two lines of a WhatsApp paste may resolve to
    the same product and both deserve to exist.

    `price_shown` is the price that was actually on the card the pharmacist
    tapped, and it is what gets stored as the claim. The storefront grid
    prices by a cheapest-fresh-offer subquery while this module prices
    through `rank()`, which also drops suspended vendors and orders a vendor
    that cannot cover the quantity behind one that can — so the two can
    differ, and when they do it is the card's number the pharmacist
    remembers. Storing our own computation instead would have moved the lie
    one screen to the left rather than ending it: the cart would have shown
    the higher number as though it had always been there. With the card's
    number stored, the cart says "was 331, now 440" and checkout refuses
    until he agrees. The server's own price is the fallback for a caller
    that sends nothing.

    It is read only when the line is created. Changing the quantity of a line
    already in the cart leaves the claim where it was, even if the caller
    sends a newer price: refreshing it would make the cart read SAME at 140
    for a line he first put in at 120, which is the silent reprice this whole
    module exists to prevent. Agreeing to a new price is a separate act, and
    `acknowledged_prices` at checkout is where it happens.
    """
    if qty < 1:
        raise ApiError("VALIDATION_ERROR", "qty_requested must be at least 1")
    if price_shown is not None and price_shown < 0:
        raise ApiError("VALIDATION_ERROR", "price_seen cannot be negative")

    # A product that is not published cannot be shown, so it cannot be in a
    # cart; without this the foreign key fails and an unknown id surfaces as
    # a 500 instead of a 404.
    published = session.execute(
        text("SELECT 1 FROM index_product WHERE id=:p AND review_status='PUBLISHED'"),
        {"p": index_product_id},
    ).scalar()
    if not published:
        raise ApiError("NOT_FOUND", "product not found")

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
             "ps": price_shown if price_shown is not None else priced.unit_price,
             "vs": priced.vendor_id},
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


def read_cart(session: Session, pharmacy_id: str) -> dict:
    """The whole cart, priced now. Every write returns this too, so the phone
    replaces what it holds instead of merging — two devices editing the same
    cart then converge on the next tap rather than drifting.

    The pricing itself is `price_request`, which the WhatsApp lane's
    confirmation screen uses as well. What a basket will cost and what a
    pasted list will cost are the same question, and answering it in two
    places is how the two lanes came to behave differently.
    """
    cart = find_cart(session, pharmacy_id)
    if cart is None:
        return {"request_id": None, "number": None, "status": None,
                "last_touched_at": None, "idle": False, "idle_hours": None,
                "lines": [], "skipped": [],
                "totals": {"goods_total": "0.00", "by_vendor": []},
                "checkout_blocked_by": []}

    priced = price_request(session, dict(cart))
    last_touched = priced["last_touched_at"]

    # `idle` is answered here, by the same config value the job reads, so the
    # app never has to invent a third notion of it. It did: the store's
    # reminder line was keyed on "touched on an earlier calendar day", which
    # fires at midnight for a basket forty minutes old and stays quiet for one
    # that has been sitting twenty-three hours.
    idle_hours = float(cfg.get(session, "CFG-CART-IDLE-HOURS", default=24))
    idle = bool(priced["lines"]) and last_touched is not None \
        and last_touched <= now() - timedelta(hours=idle_hours)

    return {
        "request_id": cart["id"], "number": cart["number"], "status": cart["status"],
        "last_touched_at": last_touched.isoformat() if last_touched else None,
        "idle": idle, "idle_hours": idle_hours,
        # Filled in by the batch endpoint when it could not add something.
        # Present and empty everywhere else, so the phone can replace the
        # whole cart on every write without a key appearing and vanishing.
        "lines": priced["lines"], "skipped": [],
        "totals": priced["totals"],
        "checkout_blocked_by": priced["checkout_blocked_by"],
    }


def on_cart_left_draft(ctx) -> None:
    """A notice about a basket stops being true the moment the basket stops
    being one.

    Without this the sequence is: Monday he fills a cart, Wednesday the idle
    job writes a notice, Wednesday afternoon he checks out from the reminder
    line at the top of the store, and Thursday he opens the bell — which is
    still showing "your basket is still waiting" — taps it, and lands on an
    empty cart, or on a different basket he started that morning. Two such
    notices read identically, so he cannot even tell which is stale.

    Registered on every SM-01 state the cart can leave DRAFT for, so no route
    out of the basket can skip it. The row is marked read rather than
    deleted: `notification` is somebody's inbox history, and a notice that
    was true when it was written stays in it.
    """
    request_id = ctx.subject_row["id"]
    ctx.session.execute(
        text("UPDATE notification SET status='READ', read_at=now(), updated_at=now() "
             "WHERE event_code='N-CART-IDLE' AND read_at IS NULL "
             "AND payload->>'request_id' = :r"),
        {"r": request_id},
    )
