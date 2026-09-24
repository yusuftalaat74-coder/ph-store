"""apply_event(session, envelope): Office -> Store events (SPEC 5.6).

Each event is applied as the SYSTEM principal (SPEC 5.9.5): state_transition
rows get actor_role = SYSTEM and notes {"via": "phoffice", "event_id": ...}.
Where a Store machine allows SYSTEM the literal "SYSTEM" actor is used; where
the Store machine only lists a human vendor role for a transition that Office
now performs (SM-03 ACCEPT/REJECT/DISPATCH, SM-05 ASSIGN/START/ATTEMPT_*),
the `OfficeActor` below carries that role *and* reports itself as SYSTEM, so
no machine table had to change and no Store user or role was added."""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.db import get_sessionmaker
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.money import quantize
from rova.domain.enums import RoleCode
from rova.domain.fsm import transition_notes
from rova.domain.machines.registry import MACHINES
from rova.fulfilment.seals import check_seals_unique
from rova.integrations.office.outbox import applying_office_event

log = logging.getLogger("rova.office")

SYSTEM = "SYSTEM"
METHOD_MAP = {"M_PESA": "MOBILE_MONEY", "E_MOLA": "MOBILE_MONEY", "MKESH": "MOBILE_MONEY",
              "BANK_TRANSFER": "BANK_TRANSFER", "CASH": "CASH_ON_DELIVERY"}


class _SystemFirst(frozenset):
    """A role set whose first element is always SYSTEM (fsm.Ctx.actor_role
    takes the first role it iterates)."""

    def __iter__(self):
        yield SYSTEM
        for r in frozenset.__iter__(self):
            if r != SYSTEM:
                yield r


@dataclass(frozen=True)
class OfficeActor:
    roles: frozenset = field(default_factory=frozenset)
    user_id: None = None


def office_actor(*roles: str) -> OfficeActor:
    return OfficeActor(roles=_SystemFirst({SYSTEM, *roles}))


DESK = office_actor(RoleCode.VENDOR_ORDER_DESK)
PICKER = office_actor(RoleCode.VENDOR_PICKER)
DISPATCHER = office_actor(RoleCode.DISPATCHER)
COURIER = office_actor(RoleCode.COURIER)
VENDOR_FINANCE = office_actor(RoleCode.VENDOR_FINANCE)


class Skip(Exception):
    """Event understood but nothing to apply (already applied / not applicable)."""


def _order(session, order_id):
    o = session.execute(text('SELECT * FROM "order" WHERE id=:o'), {"o": order_id}).mappings().first()
    if o is None:
        raise ApiError("NOT_FOUND", f"order {order_id} not found")
    return dict(o)


def _ts(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if isinstance(v, str) else v


# ------------------------------------------------------------------ handlers

def order_accepted(session, p):
    o = _order(session, p["store_order_id"])
    if o["status"] != "PENDING_ACCEPTANCE":
        raise Skip(f"order already {o['status']}")
    qty = {l["store_order_line_id"]: int(l["confirmed_qty"] or 0) for l in p["lines"]}
    for line in session.execute(text("SELECT id, ordered_qty FROM order_line WHERE order_id=:o ORDER BY created_at, id"),
                                {"o": o["id"]}).mappings().all():
        confirmed = qty.get(line["id"], 0)
        if confirmed == line["ordered_qty"]:
            MACHINES["SM-04"].apply(session, line["id"], "CONFIRM_FULL", SYSTEM)
        else:
            MACHINES["SM-04"].apply(session, line["id"], "CONFIRM_SHORT", SYSTEM, confirmed_qty=confirmed)
    MACHINES["SM-03"].apply(session, o["id"], "ACCEPT", DESK, promised_dispatch_at=_ts(p.get("promised_dispatch_at")))


def order_rejected(session, p):
    o = _order(session, p["store_order_id"])
    if o["status"] != "PENDING_ACCEPTANCE":
        raise Skip(f"order already {o['status']}")
    MACHINES["SM-03"].apply(session, o["id"], "REJECT", DESK)


def order_picked(session, p):
    o = _order(session, p["store_order_id"])
    if o["status"] != "ACCEPTED":
        raise Skip(f"order is {o['status']}")
    for l in p["lines"]:
        line = session.execute(text("SELECT * FROM order_line WHERE id=:l AND order_id=:o"),
                               {"l": l["store_order_line_id"], "o": o["id"]}).mappings().first()
        if line is None:
            raise ApiError("NOT_FOUND", f"order line {l['store_order_line_id']} not found")
        if line["picked_at"] is not None:
            continue
        seals = list(l.get("seal_ids") or [])
        if len(set(seals)) != len(seals):
            dup = sorted({x for x in seals if seals.count(x) > 1})
            raise ApiError("VALIDATION_ERROR", f"R-057: seal id(s) repeated on line {line['id']}: {', '.join(dup)}",
                           rule="R-057")
        check_seals_unique(session, o["id"], line["id"], seals)       # REVIEW F-5: same check as the pick route
        session.execute(
            text("UPDATE order_line SET batch_number=:b, lot_number=:l, expiry_date=:e, seal_ids=:s, picked_at=:n, "
                 "updated_at=:n WHERE id=:id"),
            {"b": l.get("batch_number") or l["lot_number"], "l": l["lot_number"], "e": l["expiry_date"], "s": seals,
             "n": now(), "id": line["id"]},
        )
        for seal in seals:
            session.execute(
                text("INSERT INTO traceability_event (id, order_line_id, seal_id, batch_number, lot_number, expiry_date, "
                     "event_type, actor_user_id, actor_role) VALUES (:id, :ol, :s, :b, :l, :e, 'PICKED', NULL, 'SYSTEM')"),
                {"id": new_id("trc"), "ol": line["id"], "s": seal, "b": l.get("batch_number") or l["lot_number"],
                 "l": l["lot_number"], "e": l["expiry_date"]},
            )


def order_dispatched(session, p):
    o = _order(session, p["store_order_id"])
    if o["status"] != "ACCEPTED":
        raise Skip(f"order is {o['status']}")
    MACHINES["SM-03"].apply(session, o["id"], "DISPATCH", PICKER)


def delivery_attempted(session, p):
    o = _order(session, p["store_order_id"])
    job = session.execute(text("SELECT * FROM delivery_job WHERE order_id=:o ORDER BY created_at DESC LIMIT 1"),
                          {"o": o["id"]}).mappings().first()
    if job is None:
        raise ApiError("GUARD_FAILED", "no delivery job for this order (not dispatched in PH Store yet)")
    sm05 = MACHINES["SM-05"]
    status = job["status"]
    if status in ("DELIVERED", "RETURNED_TO_VENDOR", "CANCELLED"):
        raise Skip(f"delivery job already {status}")
    if status == "CREATED":
        sm05.apply(session, job["id"], "ASSIGN", DISPATCHER)
        status = "ASSIGNED"
    if status == "FAILED_RETRY_PENDING":
        sm05.apply(session, job["id"], "RESCHEDULE", DISPATCHER)
        status = "ASSIGNED"
    if status == "ASSIGNED":
        sm05.apply(session, job["id"], "START", COURIER)
    if p["outcome"] == "DELIVERED":
        sm05.apply(session, job["id"], "ATTEMPT_DELIVERED", COURIER)
        return
    max_attempts = int(cfg.get(session, "CFG-DELIVERY-MAX-ATTEMPTS", default=3))
    count = session.execute(text("SELECT attempt_count FROM delivery_job WHERE id=:j"), {"j": job["id"]}).scalar()
    sm05.apply(session, job["id"], "ATTEMPT_FAILED_RETRY" if count + 1 < max_attempts else "ATTEMPT_FAILED_FINAL", COURIER)


def _outstanding(session, invoice):
    paid = session.execute(text("SELECT COALESCE(SUM(amount),0) FROM payment_allocation WHERE invoice_id=:i"),
                           {"i": invoice["id"]}).scalar()
    credited = session.execute(text("SELECT COALESCE(SUM(amount),0) FROM credit_note WHERE order_id=:o"),
                               {"o": invoice["order_id"]}).scalar()
    return Decimal(invoice["total_amount"]) - Decimal(paid) - Decimal(credited)


def invoice_issued(session, p):
    if session.execute(text("SELECT 1 FROM invoice WHERE office_invoice_id=:i"), {"i": p["office_invoice_id"]}).first():
        raise Skip("invoice already mirrored")
    o = _order(session, p["store_order_id"])
    inv_id = new_id("inv")
    session.execute(
        text("INSERT INTO invoice (id, order_id, issuer_vendor_id, pharmacy_id, vendor_invoice_number, total_amount, "
             "status, source, office_invoice_id) VALUES (:id, :o, :v, :p, :n, :t, 'UPLOADED', 'OFFICE', :oi)"),
        {"id": inv_id, "o": o["id"], "v": o["vendor_id"], "p": o["pharmacy_id"], "n": p["number"],
         "t": quantize(Decimal(p["gross_total"])), "oi": p["office_invoice_id"]},
    )
    for l in p["lines"]:
        session.execute(
            text("INSERT INTO invoice_line (id, invoice_id, order_line_id, invoiced_price, invoiced_qty, match_result) "
                 "VALUES (:id, :i, :ol, :pr, :q, 'OK')"),
            {"id": new_id("ivl"), "i": inv_id, "ol": l["store_order_line_id"], "pr": Decimal(l["unit_price"]),
             "q": int(l["qty"])},
        )
    # OD-11: the price is the one frozen on the order; Office never re-prices, so no re-match
    MACHINES["SM-11"].apply(session, inv_id, "MATCH_OK", SYSTEM)
    MACHINES["SM-11"].apply(session, inv_id, "FINALISE", SYSTEM)     # writes ledger_entry(INVOICE) as today


def payment_received(session, p):
    pay = session.execute(text("SELECT * FROM payment WHERE office_payment_id=:p"), {"p": p["office_payment_id"]}).mappings().first()
    if pay is None:
        pid = new_id("pay")
        session.execute(
            text("INSERT INTO payment (id, pharmacy_id, vendor_id, method, collected_by, amount, external_ref, "
                 "recorded_by_user_id, recorded_at, office_payment_id) VALUES "
                 "(:id, :ph, :v, :m, 'VENDOR_DIRECT', :a, :r, NULL, :at, :op)"),
            {"id": pid, "ph": p["store_pharmacy_id"], "v": p["store_vendor_id"], "m": METHOD_MAP[p["method"]],
             "a": quantize(Decimal(p["amount"])), "r": p.get("external_ref"), "at": _ts(p.get("received_at")) or now(),
             "op": p["office_payment_id"]},
        )
    else:
        pid = pay["id"]
    facility = session.execute(text("SELECT id FROM credit_facility WHERE vendor_id=:v AND pharmacy_id=:p"),
                               {"v": p["store_vendor_id"], "p": p["store_pharmacy_id"]}).scalar()
    applied = 0
    for a in p.get("allocations", []):
        inv = session.execute(text("SELECT * FROM invoice WHERE office_invoice_id=:i FOR UPDATE"),
                              {"i": a["office_invoice_id"]}).mappings().first()
        if inv is None:
            continue                     # e.g. an opening-balance document with no Store mirror
        target = quantize(Decimal(a["amount"]))
        existing = session.execute(text("SELECT id, amount FROM payment_allocation WHERE payment_id=:p AND invoice_id=:i"),
                                   {"p": pid, "i": inv["id"]}).mappings().first()
        delta = target - (Decimal(existing["amount"]) if existing else Decimal("0"))
        if delta <= 0:
            continue
        if existing:
            session.execute(text("UPDATE payment_allocation SET amount=:a WHERE id=:id"), {"a": target, "id": existing["id"]})
        else:
            session.execute(text("INSERT INTO payment_allocation (id, payment_id, invoice_id, amount) VALUES (:id, :p, :i, :a)"),
                            {"id": new_id("pal"), "p": pid, "i": inv["id"], "a": target})
        if facility:
            session.execute(
                text("INSERT INTO ledger_entry (id, credit_facility_id, entry_type, reference_type, reference_id, amount) "
                     "VALUES (:id, :f, 'PAYMENT', 'payment', :r, :a)"), {"id": new_id("led"), "f": facility, "r": pid, "a": -delta})
        if inv["status"] in ("AWAITING_PAYMENT", "PARTIALLY_PAID"):
            MACHINES["SM-11"].apply(session, inv["id"], "ALLOCATE_FULL" if _outstanding(session, inv) <= 0
                                    else "ALLOCATE_PARTIAL", SYSTEM)
        applied += 1
    return applied


def credit_note_issued(session, p):
    """SPEC 5.6 (v1.1, REVIEW F-1): mirror an Office credit note and close the
    Store return it answers. PH Office owns credit notes; the Store row exists
    so exposure/ledger and SM-10 agree with the books."""
    if session.execute(text("SELECT 1 FROM credit_note WHERE office_credit_note_id=:c"),
                       {"c": p["office_credit_note_id"]}).first():
        raise Skip("credit note already mirrored")
    o = _order(session, p["store_order_id"])
    ret = None
    if p.get("return_ref"):
        ret = session.execute(
            text('SELECT id, status FROM "return" WHERE order_id=:o AND (id=:r OR rma_number=:r) FOR UPDATE'),
            {"o": o["id"], "r": p["return_ref"]}).mappings().first()
    if ret is None:
        # ck_credit_note_origin: a Store credit note must cite a return or a
        # dispute. A GOODWILL / PRICE_CORRECTION note from Office cites
        # neither; the balance is corrected by the account.updated that follows.
        raise Skip(f"credit note {p.get('number') or p['office_credit_note_id']} ({p.get('reason')}) cites no Store "
                   f"return (return_ref={p.get('return_ref')!r}); not mirrored — balance follows account.updated")
    amount = quantize(Decimal(p["gross_total"]))
    cid = new_id("crn")
    session.execute(
        text("INSERT INTO credit_note (id, vendor_id, pharmacy_id, order_id, return_id, dispute_id, amount, issued_at, "
             "office_credit_note_id) VALUES (:id, :v, :p, :o, :r, NULL, :a, :t, :oc)"),
        {"id": cid, "v": o["vendor_id"], "p": o["pharmacy_id"], "o": o["id"], "r": ret["id"], "a": amount,
         "t": _ts(p.get("issued_at")) or now(), "oc": p["office_credit_note_id"]},
    )
    facility = session.execute(text("SELECT id FROM credit_facility WHERE vendor_id=:v AND pharmacy_id=:p"),
                               {"v": o["vendor_id"], "p": o["pharmacy_id"]}).scalar()
    if facility:
        session.execute(
            text("INSERT INTO ledger_entry (id, credit_facility_id, entry_type, reference_type, reference_id, amount) "
                 "VALUES (:id, :f, 'CREDIT_NOTE', 'credit_note', :r, :a)"),
            {"id": new_id("led"), "f": facility, "r": cid, "a": -amount})
    if ret["status"] == "RECEIVED_BY_VENDOR":
        MACHINES["SM-10"].apply(session, ret["id"], "CLOSE_WITH_CREDIT_NOTE", VENDOR_FINANCE)


def payment_reversed(session, p):
    """SPEC 5.6 (REVIEW F-1): undo a mirrored Office payment — an ADJUSTMENT
    ledger entry that puts back exactly what the mirror took off the facility,
    the mirror allocations removed, and SM-11 moved back for each invoice."""
    pay = session.execute(text("SELECT * FROM payment WHERE office_payment_id=:p FOR UPDATE"),
                          {"p": p["office_payment_id"]}).mappings().first()
    if pay is None:
        raise Skip("payment was never mirrored in PH Store; balance follows account.updated")
    if session.execute(text("SELECT 1 FROM ledger_entry WHERE entry_type='ADJUSTMENT' AND reference_type='payment' "
                            "AND reference_id=:p"), {"p": pay["id"]}).first():
        raise Skip("payment reversal already applied")
    facility = session.execute(text("SELECT id FROM credit_facility WHERE vendor_id=:v AND pharmacy_id=:p"),
                               {"v": pay["vendor_id"], "p": pay["pharmacy_id"]}).scalar()
    # payment.received only moved the ledger for the part allocated to mirrored
    # invoices, so the reversal restores that part (== amount when fully allocated)
    taken = session.execute(text("SELECT COALESCE(SUM(amount), 0) FROM ledger_entry WHERE entry_type='PAYMENT' "
                                 "AND reference_type='payment' AND reference_id=:p"), {"p": pay["id"]}).scalar()
    if facility and Decimal(taken) != 0:
        session.execute(
            text("INSERT INTO ledger_entry (id, credit_facility_id, entry_type, reference_type, reference_id, amount) "
                 "VALUES (:id, :f, 'ADJUSTMENT', 'payment', :r, :a)"),
            {"id": new_id("led"), "f": facility, "r": pay["id"], "a": -Decimal(taken)})
    allocs = session.execute(text("SELECT * FROM payment_allocation WHERE payment_id=:p ORDER BY created_at, id"),
                             {"p": pay["id"]}).mappings().all()
    token = transition_notes.set({**(transition_notes.get() or {}), "reversed_payment": pay["id"],
                                  "reason": p.get("reason")})
    try:
        for a in allocs:
            session.execute(text("DELETE FROM payment_allocation WHERE id=:id"), {"id": a["id"]})
            inv = session.execute(text("SELECT * FROM invoice WHERE id=:i FOR UPDATE"),
                                  {"i": a["invoice_id"]}).mappings().one()
            if inv["status"] not in ("PAID", "PARTIALLY_PAID") or _outstanding(session, inv) <= 0:
                continue
            still_paid = session.execute(text("SELECT COALESCE(SUM(amount),0) FROM payment_allocation WHERE invoice_id=:i"),
                                         {"i": inv["id"]}).scalar()
            MACHINES["SM-11"].apply(session, inv["id"], "UNALLOCATE_PARTIAL" if Decimal(still_paid) > 0
                                    else "UNALLOCATE_ALL", SYSTEM)
    finally:
        transition_notes.reset(token)


def account_updated(session, p):
    session.execute(
        text("UPDATE credit_facility SET office_balance=:b, office_credit_limit=:l, office_hold=:h, office_synced_at=:n, "
             "updated_at=:n WHERE vendor_id=:v AND pharmacy_id=:p"),
        {"b": quantize(Decimal(p["balance"])), "l": quantize(Decimal(p["credit_limit"])), "h": p["status"] != "ACTIVE",
         "n": now(), "v": p["store_vendor_id"], "p": p["store_pharmacy_id"]},
    )
    snap = session.execute(text("SELECT office_account_snapshot FROM pharmacy_account WHERE id=:p FOR UPDATE"),
                           {"p": p["store_pharmacy_id"]}).scalar() or {}
    snap.setdefault("vendors", {})[p["store_vendor_id"]] = p
    snap["updated_at"] = now().isoformat()
    session.execute(text("UPDATE pharmacy_account SET office_account_snapshot=CAST(:s AS JSONB) WHERE id=:p"),
                    {"s": json.dumps(snap), "p": p["store_pharmacy_id"]})


HANDLERS = {
    "order.accepted": order_accepted,
    "order.rejected": order_rejected,
    "order.picked": order_picked,
    "order.dispatched": order_dispatched,
    "delivery.attempted": delivery_attempted,
    "invoice.issued": invoice_issued,
    "payment.received": payment_received,
    "account.updated": account_updated,
    "credit_note.issued": credit_note_issued,
    "payment.reversed": payment_reversed,
}


# ------------------------------------------------------------------ record / apply

def record(session: Session, env: dict) -> tuple[str, bool]:
    missing = [k for k in ("event_id", "event_type", "aggregate_type", "aggregate_id", "aggregate_version", "payload")
               if k not in env]
    if missing:
        raise ApiError("VALIDATION_ERROR", f"envelope is missing {missing}")
    existing = session.execute(text("SELECT id FROM integration_inbound_event WHERE event_id=:e"),
                               {"e": env["event_id"]}).scalar()
    if existing:
        return existing, True
    iid = new_id("ibx")
    try:
        with session.begin_nested():
            session.execute(
                text("INSERT INTO integration_inbound_event (id, event_id, event_type, seq, aggregate_type, aggregate_id, "
                     "aggregate_version, payload) VALUES (:id, :e, :t, :s, :at, :aid, :v, CAST(:p AS JSONB))"),
                {"id": iid, "e": env["event_id"], "t": env["event_type"], "s": env.get("seq"), "at": env["aggregate_type"],
                 "aid": env["aggregate_id"], "v": int(env["aggregate_version"]), "p": json.dumps(env["payload"])},
            )
    except IntegrityError:
        return session.execute(text("SELECT id FROM integration_inbound_event WHERE event_id=:e"),
                               {"e": env["event_id"]}).scalar(), True
    return iid, False


def _mark(session, iid, status, error=None):
    session.execute(text("UPDATE integration_inbound_event SET status=:s, error=:e, processed_at=:n WHERE id=:id"),
                    {"s": status, "e": error, "n": now() if status != "FAILED" else None, "id": iid})


def apply_event(session: Session, inbound_id: str) -> str:
    ev = session.execute(text("SELECT * FROM integration_inbound_event WHERE id=:i FOR UPDATE"),
                         {"i": inbound_id}).mappings().one()
    if ev["status"] not in ("RECEIVED", "FAILED"):
        return ev["status"]
    handler = HANDLERS.get(ev["event_type"])
    if handler is None:
        log.warning("office event %s (%s) not applied in this version", ev["event_id"], ev["event_type"])
        return ev["status"]
    last = session.execute(
        text("SELECT MAX(aggregate_version) FROM integration_inbound_event WHERE aggregate_type=:t AND aggregate_id=:a "
             "AND status='PROCESSED' AND id <> :i"), {"t": ev["aggregate_type"], "a": ev["aggregate_id"], "i": inbound_id},
    ).scalar()
    if last is not None and ev["aggregate_version"] <= last:
        _mark(session, inbound_id, "SKIPPED", f"stale version {ev['aggregate_version']} <= {last}")
        return "SKIPPED"
    if last is not None and ev["aggregate_version"] > last + 1:
        log.warning("office event %s: version gap %s after %s", ev["event_id"], ev["aggregate_version"], last)
    t1 = transition_notes.set({"via": "phoffice", "event_id": ev["event_id"], "event_type": ev["event_type"]})
    t2 = applying_office_event.set(ev["event_id"])
    try:
        handler(session, ev["payload"])
        _mark(session, inbound_id, "PROCESSED")
        return "PROCESSED"
    except Skip as s:
        _mark(session, inbound_id, "SKIPPED", str(s))
        return "SKIPPED"
    finally:
        transition_notes.reset(t1)
        applying_office_event.reset(t2)


def apply_in_new_transaction(inbound_id: str) -> str:
    sm = get_sessionmaker()
    session = sm()
    try:
        status = apply_event(session, inbound_id)
        session.commit()
        return status
    except (ApiError, DBAPIError, KeyError, ValueError, TypeError) as exc:
        session.rollback()
        msg = exc.message if isinstance(exc, ApiError) else str(getattr(exc, "orig", exc)).splitlines()[0]
        log.warning("office event %s failed: %s", inbound_id, msg)
        s2 = sm()
        try:
            _mark(s2, inbound_id, "FAILED", msg[:2000])
            s2.commit()
        finally:
            s2.close()
        return "FAILED"
    finally:
        session.close()


def retry_failed(limit: int = 100) -> int:
    s = get_sessionmaker()()
    try:
        ids = s.execute(text("SELECT id FROM integration_inbound_event WHERE status='FAILED' ORDER BY seq NULLS LAST, "
                             "received_at LIMIT :n"), {"n": limit}).scalars().all()
    finally:
        s.close()
    return sum(1 for i in ids if apply_in_new_transaction(i) == "PROCESSED")
