"""The assistant layer.

What this is, stated plainly so nobody mistakes it for something else: a set
of endpoints that compute suggestions from the pharmacy's own data and label
every one with the evidence it came from. There is no language model behind
it, and it does not pretend there is.

That is a deliberate choice rather than a shortcut. The product idea is "know
the pharmacy better than it knows itself" — replenishment timing, demand
patterns, what is quietly going out of stock. All of that is arithmetic over
dispensing history and the order book. Wrapping arithmetic in generated prose
would add a way to be confidently wrong about a medicine, and would make the
suggestion impossible to audit. So every response here carries `basis`: the
rows the number came from, and the window it covers.

Three hard rules:

* A suggestion is never an order. Nothing in this module writes to `request`,
  `order` or anything downstream — the pharmacist takes the suggestion to the
  ordinary intake surface, or ignores it.
* A suggestion with too little evidence says so, with `confidence: "NONE"`
  and the sample size, rather than producing a plausible number from three
  data points.
* A pharmacy only ever sees its own data. There is no cross-pharmacy
  aggregate exposed here, because that is another pharmacy's commercial
  information.
"""
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.core.clock import now
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.core.money import money_str
from rova.domain.enums import RoleCode

router = APIRouter(prefix="/v1/assistant", tags=["assistant"])

_PHARMACY = (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER)
_PHARMACY_OR_OPS = _PHARMACY + (RoleCode.OPS_REVIEWER, RoleCode.PLATFORM_ADMIN)

# Below this many observed periods a rate is noise, not a trend.
MIN_PERIODS_FOR_A_RATE = 14
MIN_ORDERS_FOR_A_PATTERN = 3


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _pharmacy_scope(principal: Principal, explicit: str | None, session: Session) -> str:
    if principal.pharmacy_id:
        if explicit and explicit != principal.pharmacy_id:
            raise ApiError("FORBIDDEN", "a pharmacy can only see its own data")
        return principal.pharmacy_id
    if not explicit:
        raise ApiError("VALIDATION_ERROR", "pharmacy_id is required for a platform principal")
    if not session.execute(text("SELECT 1 FROM pharmacy_account WHERE id=:p"), {"p": explicit}).first():
        raise ApiError("NOT_FOUND", "pharmacy not found")
    return explicit


def _confidence(sample: int, minimum: int) -> str:
    if sample == 0:
        return "NONE"
    if sample < minimum:
        return "LOW"
    if sample < minimum * 3:
        return "MEDIUM"
    return "HIGH"


@router.get("/replenishment")
def replenishment(pharmacy_id: str | None = None,
                  window_days: int = Query(default=90, ge=7, le=365),
                  principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS)),
                  session: Session = Depends(get_session, scope="function")):
    """When each product is likely to run out, from the pharmacy's own
    dispensing feed.

    Requires a linked PMS: without a dispensing feed there is no consumption
    rate, and this returns an empty list with the reason rather than guessing
    consumption from purchase history — what a pharmacy bought is not what it
    dispensed.
    """
    pid = _pharmacy_scope(principal, pharmacy_id, session)
    link = session.execute(
        text("SELECT id, status FROM pms_link WHERE pharmacy_id=:p AND status='LINKED'"), {"p": pid},
    ).mappings().first()
    if link is None:
        return {"pharmacy_id": pid, "items": [],
                "reason": "no LINKED pms_link: there is no dispensing feed to compute a consumption rate from",
                "basis": {"source": "dispensing_aggregate", "rows": 0}}

    since = (now() - timedelta(days=window_days)).date()
    rows = session.execute(
        text("SELECT index_product_id, count(*) AS periods, SUM(dispensed_units) AS units, "
             "MAX(period_date) AS latest, "
             "(SELECT stock_on_hand FROM dispensing_aggregate d2 WHERE d2.pms_link_id = d.pms_link_id "
             " AND d2.index_product_id = d.index_product_id ORDER BY period_date DESC LIMIT 1) AS stock_on_hand "
             "FROM dispensing_aggregate d WHERE d.pms_link_id=:l AND d.period_date >= :since "
             "GROUP BY d.pms_link_id, d.index_product_id ORDER BY units DESC"),
        {"l": link["id"], "since": since},
    ).mappings().all()

    items = []
    for r in rows:
        periods, units = r["periods"], int(r["units"] or 0)
        per_day = Decimal(units) / Decimal(periods) if periods else Decimal(0)
        stock = r["stock_on_hand"]
        days_left = None
        if stock is not None and per_day > 0:
            days_left = int(Decimal(stock) / per_day)
        items.append({
            "index_product_id": r["index_product_id"],
            "units_dispensed": units,
            "periods_observed": periods,
            "daily_rate": str(round(per_day, 2)),
            "stock_on_hand": stock,
            "estimated_days_of_cover": days_left,
            "confidence": _confidence(periods, MIN_PERIODS_FOR_A_RATE),
            # every row carries what it was computed from
            "basis": {"source": "dispensing_aggregate", "window_days": window_days,
                      "periods": periods, "latest_period": r["latest"]},
        })
    items.sort(key=lambda i: (i["estimated_days_of_cover"] is None, i["estimated_days_of_cover"] or 0))
    return {"pharmacy_id": pid, "window_days": window_days, "items": items}


@router.get("/order-patterns")
def order_patterns(pharmacy_id: str | None = None,
                   window_days: int = Query(default=180, ge=30, le=730),
                   principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS)),
                   session: Session = Depends(get_session, scope="function")):
    """How this pharmacy actually orders: cadence, basket size, and which
    products repeat. Computed from its own confirmed orders."""
    pid = _pharmacy_scope(principal, pharmacy_id, session)
    since = now() - timedelta(days=window_days)
    orders = session.execute(
        text('SELECT id, created_at, goods_total FROM "order" WHERE pharmacy_id=:p AND created_at >= :s '
             "AND status NOT IN ('CANCELLED','REJECTED') ORDER BY created_at ASC"),
        {"p": pid, "s": since},
    ).mappings().all()
    n = len(orders)
    if n < MIN_ORDERS_FOR_A_PATTERN:
        return {"pharmacy_id": pid, "window_days": window_days, "order_count": n,
                "confidence": _confidence(n, MIN_ORDERS_FOR_A_PATTERN),
                "reason": f"{n} order(s) in the window is not enough to describe a pattern",
                "items": []}

    gaps = [(orders[i]["created_at"] - orders[i - 1]["created_at"]).days for i in range(1, n)]
    avg_gap = sum(gaps) / len(gaps) if gaps else None
    total = sum(Decimal(o["goods_total"]) for o in orders)

    repeats = session.execute(
        text('SELECT ol.index_product_id, count(DISTINCT ol.order_id) AS orders_containing, '
             "SUM(ol.ordered_qty) AS units FROM order_line ol "
             'JOIN "order" o ON o.id = ol.order_id '
             "WHERE o.pharmacy_id=:p AND o.created_at >= :s AND o.status NOT IN ('CANCELLED','REJECTED') "
             "GROUP BY ol.index_product_id ORDER BY orders_containing DESC LIMIT 25"),
        {"p": pid, "s": since},
    ).mappings().all()

    return {
        "pharmacy_id": pid, "window_days": window_days, "order_count": n,
        "average_days_between_orders": round(avg_gap, 1) if avg_gap is not None else None,
        "average_basket_value": money_str(total / n),
        "confidence": _confidence(n, MIN_ORDERS_FOR_A_PATTERN),
        "items": [{"index_product_id": r["index_product_id"],
                   "orders_containing": r["orders_containing"],
                   "units": int(r["units"]),
                   "repeat_share": round(r["orders_containing"] / n, 2)} for r in repeats],
        "basis": {"source": "order + order_line", "orders": n, "since": since},
    }


@router.get("/stock-risks")
def stock_risks(pharmacy_id: str | None = None,
                principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS)),
                session: Session = Depends(get_session, scope="function")):
    """Products this pharmacy buys that are currently hard to supply — no
    fresh offer, or its recent lines were short or dropped. This is the
    warning that matters before a stock-out, and it comes entirely from
    events that already happened."""
    pid = _pharmacy_scope(principal, pharmacy_id, session)
    since = now() - timedelta(days=90)
    rows = session.execute(
        text("SELECT ol.index_product_id, "
             "count(*) FILTER (WHERE ol.fulfilment_status IN ('LINE_SHORT','LINE_DROPPED')) AS short_lines, "
             "count(*) AS total_lines, "
             "(SELECT count(*) FROM vendor_offer vo WHERE vo.index_product_id = ol.index_product_id "
             " AND vo.freshness_state='FRESH' AND vo.qty_available > 0) AS fresh_offers "
             'FROM order_line ol JOIN "order" o ON o.id = ol.order_id '
             "WHERE o.pharmacy_id=:p AND o.created_at >= :s "
             "GROUP BY ol.index_product_id HAVING count(*) > 0 ORDER BY 2 DESC LIMIT 50"),
        {"p": pid, "s": since},
    ).mappings().all()
    items = []
    for r in rows:
        risk = "NONE"
        if r["fresh_offers"] == 0:
            risk = "HIGH"
        elif r["short_lines"] and r["short_lines"] / r["total_lines"] >= Decimal("0.3"):
            risk = "MEDIUM"
        elif r["short_lines"]:
            risk = "LOW"
        if risk == "NONE":
            continue
        items.append({"index_product_id": r["index_product_id"], "risk": risk,
                      "short_lines": r["short_lines"], "total_lines": r["total_lines"],
                      "fresh_offers": r["fresh_offers"],
                      "basis": {"source": "order_line + vendor_offer", "window_days": 90}})
    return {"pharmacy_id": pid, "items": items}


@router.get("/savings")
def savings(pharmacy_id: str | None = None,
            principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS)),
            session: Session = Depends(get_session, scope="function")):
    """Where this pharmacy paid more than the cheapest fresh offer at the
    time of ordering — and, just as importantly, where it could not have paid
    less because the price is fixed by law.

    A regulated product is excluded from the comparison entirely, with its
    count reported. Suggesting a "saving" on a state-priced medicine would be
    suggesting an illegal transaction.

    One entry per product, not per order line: a product bought three times
    was three entries, and the screen showing them all as separate cards was
    the visible half of the problem. The invisible half is worse — see the
    comment on the query.
    """
    pid = _pharmacy_scope(principal, pharmacy_id, session)
    since = now() - timedelta(days=90)
    # One row per product, carrying the *most recent* price paid for it.
    #
    # Per line was wrong in a way that only shows on a pharmacy with some
    # history: this endpoint emits a line only where today's best beats that
    # line's own price, so the line that contradicts the card is the one the
    # caller never sees. Paid 120 in July, 95 last week, 100 today — the July
    # line qualifies, the card says "you paid 120, now 100", and he paid 95
    # nine days ago. The newest price is the one a comparison is worth making
    # against, and `ordered_at` travels with it so the screen can say when.
    rows = session.execute(
        text("SELECT DISTINCT ON (ol.index_product_id) "
             "       ol.index_product_id, ol.regulated_price, ol.unit_price, ol.confirmed_qty, "
             "       o.created_at AS ordered_at, "
             "(SELECT MIN(vo.price) FROM vendor_offer vo WHERE vo.index_product_id = ol.index_product_id "
             " AND vo.freshness_state='FRESH' AND vo.price IS NOT NULL) AS best_fresh_price "
             'FROM order_line ol JOIN "order" o ON o.id = ol.order_id '
             "WHERE o.pharmacy_id=:p AND o.created_at >= :s AND ol.confirmed_qty > 0 "
             "ORDER BY ol.index_product_id, o.created_at DESC"),
        {"p": pid, "s": since},
    ).mappings().all()

    regulated_lines = sum(1 for r in rows if r["regulated_price"])
    opportunities, total = [], Decimal("0")
    for r in rows:
        if r["regulated_price"] or r["best_fresh_price"] is None:
            continue
        paid, best = Decimal(r["unit_price"]), Decimal(r["best_fresh_price"])
        if best < paid:
            delta = (paid - best) * r["confirmed_qty"]
            total += delta
            opportunities.append({"index_product_id": r["index_product_id"],
                                  "unit_price_paid": money_str(paid),
                                  "best_fresh_price": money_str(best),
                                  "qty": r["confirmed_qty"],
                                  "ordered_at": r["ordered_at"].isoformat(),
                                  "difference": money_str(delta)})
    opportunities.sort(key=lambda o: Decimal(o["difference"]), reverse=True)
    return {
        "pharmacy_id": pid, "window_days": 90,
        "lines_examined": len(rows),
        "regulated_lines_excluded": regulated_lines,
        "regulated_note_pt": "Os preços de medicamentos regulados são fixados por lei "
                             "(Diploma Ministerial 21/2017) e não entram nesta comparação.",
        "total_difference": money_str(total),
        "items": opportunities[:25],
        "basis": {"source": "order_line vs current FRESH vendor_offer",
                  "caveat": "compares against today's fresh offers, not the offers live at order time"},
    }


@router.get("/summary")
def summary(pharmacy_id: str | None = None,
            principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS)),
            session: Session = Depends(get_session, scope="function")):
    """One call that answers "how am I doing" — what is in flight, what is
    owed, what is at risk. Every figure is a count or a sum of rows the
    pharmacy can go and look at."""
    pid = _pharmacy_scope(principal, pharmacy_id, session)
    counts = session.execute(
        # The cart is a DRAFT request, so it would otherwise be counted as an
        # open request and the pharmacist would see "1 open request" for a
        # basket they have not sent. It is reported separately as `cart_lines`.
        text('SELECT '
             ' (SELECT count(*) FROM request WHERE pharmacy_id=:p '
             "    AND status NOT IN ('CLOSED','CANCELLED') "
             "    AND NOT (is_cart AND status = 'DRAFT')) "
             '   AS open_requests,'
             ' (SELECT count(*) FROM request_line rl JOIN request r ON r.id = rl.request_id '
             "    WHERE r.pharmacy_id=:p AND r.is_cart AND r.status='DRAFT' "
             "    AND rl.line_kind='CATALOGUE') AS cart_lines,"
             ' (SELECT count(*) FROM "order" WHERE pharmacy_id=:p AND status NOT IN (\'CLOSED\',\'CANCELLED\',\'REJECTED\')) '
             '   AS open_orders,'
             ' (SELECT count(*) FROM dispute d JOIN "order" o ON o.id=d.order_id WHERE o.pharmacy_id=:p '
             "    AND d.status <> 'RESOLVED') AS open_disputes,"
             ' (SELECT count(*) FROM return r JOIN "order" o ON o.id=r.order_id WHERE o.pharmacy_id=:p '
             "    AND r.status NOT IN ('CLOSED','REJECTED')) AS open_returns"),
        {"p": pid},
    ).mappings().one()
    owed = session.execute(
        text("SELECT COALESCE(SUM(i.total_amount), 0) - COALESCE(("
             "  SELECT SUM(pa.amount) FROM payment_allocation pa JOIN invoice i2 ON i2.id = pa.invoice_id "
             "  WHERE i2.pharmacy_id=:p), 0) AS outstanding "
             "FROM invoice i WHERE i.pharmacy_id=:p AND i.status IN ('AWAITING_PAYMENT','PARTIALLY_PAID')"),
        {"p": pid},
    ).scalar()
    return {"pharmacy_id": pid, "generated_at": now(), **dict(counts),
            "outstanding_to_vendors": money_str(Decimal(owed or 0)),
            "basis": {"source": "request, order, dispute, return, invoice, payment_allocation"}}


class Ask(Body):
    question: str = Field(min_length=1, max_length=500)
    pharmacy_id: str | None = None


_INTENTS = (
    ("replenish", ("repor", "repo", "acabar", "stock", "estoque", "quando", "نفاد", "مخزون")),
    ("savings", ("preço", "preco", "barato", "poupar", "caro", "سعر", "أرخص", "توفير")),
    ("risk", ("falta", "risco", "rutura", "esgotado", "نقص", "خطر")),
    ("summary", ("resumo", "situação", "situacao", "dívida", "divida", "ملخص", "حسابي", "مديونية")),
)


@router.post("/ask")
def ask(body: Ask,
        principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS)),
        session: Session = Depends(get_session, scope="function")):
    """Routes a typed question to the computation that answers it.

    This is keyword routing over the four endpoints above, and the response
    says so in `answered_by`. It does not generate text. When no intent
    matches, it returns the list of what it can answer instead of inventing
    a reply — a wrong answer about a medicine is worse than "I don't know".
    """
    pid = _pharmacy_scope(principal, body.pharmacy_id, session)
    q = body.question.lower()
    intent = next((name for name, words in _INTENTS if any(w in q for w in words)), None)

    if intent == "replenish":
        return {"intent": intent, "answered_by": "GET /v1/assistant/replenishment",
                "result": replenishment(pid, 90, principal, session)}
    if intent == "savings":
        return {"intent": intent, "answered_by": "GET /v1/assistant/savings",
                "result": savings(pid, principal, session)}
    if intent == "risk":
        return {"intent": intent, "answered_by": "GET /v1/assistant/stock-risks",
                "result": stock_risks(pid, principal, session)}
    if intent == "summary":
        return {"intent": intent, "answered_by": "GET /v1/assistant/summary",
                "result": summary(pid, principal, session)}

    return {
        "intent": None,
        "answered_by": None,
        "message_pt": "Não consigo responder a isso com os seus dados. Posso ajudar com: "
                      "reposição, poupança, risco de rutura, e resumo da conta.",
        "message_ar": "مش قادر أجاوب على دي من بياناتك. أقدر أساعد في: "
                      "إعادة التموين، التوفير، خطر النفاد، وملخص الحساب.",
        "capabilities": ["replenishment", "savings", "stock-risks", "summary"],
        "escalate_to": "POST /v1/support/threads/me/escalate",
    }


@router.get("/capabilities")
def capabilities(principal: Principal = Depends(require_roles(*_PHARMACY_OR_OPS))):
    """What the assistant can and cannot do, stated outright. An honest
    capability list is what keeps a pharmacist from asking it a clinical
    question."""
    return {
        "computes": [
            {"name": "replenishment", "needs": "a LINKED pms_link with dispensing_aggregate rows"},
            {"name": "order-patterns", "needs": f"at least {MIN_ORDERS_FOR_A_PATTERN} orders in the window"},
            {"name": "stock-risks", "needs": "order history"},
            {"name": "savings", "needs": "order history and fresh offers; regulated products excluded"},
            {"name": "summary", "needs": "nothing"},
        ],
        "does_not": [
            "generate free text",
            "give clinical, dosing or substitution advice",
            "place, change or cancel an order",
            "show another pharmacy's data",
            "suggest a different price on a regulated product",
        ],
        "every_result_carries": "a `basis` naming the rows and window it was computed from",
    }
