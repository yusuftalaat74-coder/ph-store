"""A15.4 billing rows (138-143 reduced set) + A10/A11.5. Item-3 fix
(backend-review-r1.md): without this the credit gate could never release
exposure — `pending_exposure` only excludes an order once its invoice
reaches `price_match_flag='OK'` (A3.3), and nothing ever wrote that invoice
or its `ledger_entry(INVOICE)`/`ledger_entry(PAYMENT)` rows."""
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.money import quantize
from rova.domain.enums import RoleCode
from rova.domain.machines.registry import MACHINES
from rova.integrations.office.router import require_not_office_managed  # PH Office SPEC 5.9.6

router = APIRouter(prefix="/v1", tags=["billing"])

_VENDOR_FINANCE = (RoleCode.VENDOR_FINANCE, RoleCode.VENDOR_ADMIN)
_INVOICE_READERS = (RoleCode.PHARMACY_ADMIN, RoleCode.VENDOR_FINANCE, RoleCode.VENDOR_ADMIN,
                     RoleCode.COMPLIANCE_OFFICER, RoleCode.PLATFORM_FINANCE, RoleCode.OPS_REVIEWER)
_PAYMENT_WRITERS = (RoleCode.VENDOR_FINANCE, RoleCode.VENDOR_ADMIN, RoleCode.PLATFORM_FINANCE)
_PAYMENT_READERS = (RoleCode.PHARMACY_ADMIN, RoleCode.VENDOR_FINANCE, RoleCode.VENDOR_ADMIN, RoleCode.PLATFORM_FINANCE)


def _money(v) -> str | None:
    return str(v) if v is not None else None


def _invoice_out(row: dict) -> dict:
    out = dict(row)
    out["total_amount"] = _money(out.get("total_amount"))
    return out


def _invoice_or_404(session, invoice_id, principal):
    row = session.execute(text("SELECT * FROM invoice WHERE id=:id"), {"id": invoice_id}).mappings().first()
    if row is None:
        raise ApiError("NOT_FOUND", "invoice not found")
    owns = (principal.pharmacy_id and principal.pharmacy_id == row["pharmacy_id"]) or \
           (principal.vendor_id and principal.vendor_id == row["issuer_vendor_id"]) or \
           principal.scope().kind == "platform"
    if not owns:
        raise ApiError("NOT_FOUND", "invoice not found")
    return row


class InvoiceLineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_line_id: str
    invoiced_price: Decimal
    invoiced_qty: int


class InvoiceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_invoice_number: str
    lines: list[InvoiceLineBody]
    document_ref: str | None = None


@router.post("/orders/{order_id}/invoice", dependencies=[Depends(require_not_office_managed)])
def upload_invoice(order_id: str, body: InvoiceBody, principal: Principal = Depends(require_roles(*_VENDOR_FINANCE)),
                    session: Session = Depends(get_session, scope="function")):
    """A8.6/A10: computes each line's `match_result` against the price frozen
    at order creation (regulated: `price_reference.wholesale_derived_price`
    effective on the order's `created_at`, zero tolerance, R-006; free:
    `order_line.unit_price`), picks `MATCH_OK`/`MATCH_DEVIATION` the same way
    `fulfilment/router.py::accept_order` picks ACCEPT/REJECT for SM-03 (A4.1
    fixed-`to_state`-per-trigger), then runs `FINALISE` automatically on a
    clean match (no human step between, A4.2) — which is what writes the
    `ledger_entry(INVOICE)` the credit gate depends on."""
    order = session.execute(text('SELECT * FROM "order" WHERE id=:o'), {"o": order_id}).mappings().first()
    if order is None:
        raise ApiError("NOT_FOUND", "order not found")
    if principal.vendor_id != order["vendor_id"]:
        raise ApiError("NOT_FOUND", "order not found")
    if order["status"] not in ("RECEIPT_ACCEPTED", "DISPUTED", "RETURN_IN_PROGRESS"):
        raise ApiError("GUARD_FAILED", "order has not reached RECEIPT_ACCEPTED yet")
    existing = session.execute(
        text("SELECT 1 FROM invoice WHERE order_id=:o AND status <> 'WRITTEN_OFF'"), {"o": order_id}
    ).first()
    if existing:
        raise ApiError("GUARD_FAILED", "an invoice already exists for this order")
    if not body.lines:
        raise ApiError("VALIDATION_ERROR", "lines must not be empty")

    total = quantize(sum((l.invoiced_price * l.invoiced_qty for l in body.lines), Decimal("0")))
    invoice_id = new_id("inv")
    session.execute(
        text(
            "INSERT INTO invoice (id, order_id, issuer_vendor_id, pharmacy_id, vendor_invoice_number, "
            "total_amount, status, document_ref) VALUES "
            "(:id, :o, :v, :p, :num, :tot, 'UPLOADED', :doc)"
        ),
        {"id": invoice_id, "o": order_id, "v": order["vendor_id"], "p": order["pharmacy_id"],
         "num": body.vendor_invoice_number, "tot": total, "doc": body.document_ref},
    )

    all_ok = True
    deviation_flag = "DEVIATION_ABOVE"
    for l in body.lines:
        ol = session.execute(
            text("SELECT * FROM order_line WHERE id=:id AND order_id=:o"), {"id": l.order_line_id, "o": order_id}
        ).mappings().first()
        if ol is None:
            raise ApiError("NOT_FOUND", "order line not found")
        if ol["regulated_price"]:
            expected = session.execute(
                text(
                    "SELECT wholesale_derived_price FROM price_reference WHERE index_product_id=:p "
                    "AND effective_from <= :d ORDER BY effective_from DESC LIMIT 1"
                ),
                {"p": ol["index_product_id"], "d": order["created_at"].date()},
            ).scalar()
        else:
            expected = ol["unit_price"]
        if expected is None:
            match = "NOT_APPLICABLE"
        elif l.invoiced_price == expected:
            match = "OK"
        elif l.invoiced_price > expected:
            match = "DEVIATION_ABOVE"
            all_ok = False
            deviation_flag = "DEVIATION_ABOVE"
        else:
            match = "DEVIATION_BELOW"
            all_ok = False
            deviation_flag = "DEVIATION_BELOW"
        session.execute(
            text(
                "INSERT INTO invoice_line (id, invoice_id, order_line_id, invoiced_price, invoiced_qty, "
                "match_result) VALUES (:id, :inv, :ol, :price, :qty, :match)"
            ),
            {"id": new_id("ivl"), "inv": invoice_id, "ol": l.order_line_id, "price": l.invoiced_price,
             "qty": l.invoiced_qty, "match": match},
        )

    if all_ok:
        MACHINES["SM-11"].apply(session, invoice_id, "MATCH_OK", "SYSTEM")
        updated = MACHINES["SM-11"].apply(session, invoice_id, "FINALISE", "SYSTEM")
    else:
        updated = MACHINES["SM-11"].apply(session, invoice_id, "MATCH_DEVIATION", "SYSTEM",
                                           price_match_flag=deviation_flag)
    return _invoice_out(dict(updated))


@router.get("/invoices")
def list_invoices(status: str | None = None, price_match_flag: str | None = None,
                   principal: Principal = Depends(require_roles(*_INVOICE_READERS)),
                   session: Session = Depends(get_session, scope="function")):
    clauses, params = [], {}
    if principal.scope().kind == "pharmacy":
        clauses.append("pharmacy_id=:ph")
        params["ph"] = principal.pharmacy_id
    elif principal.scope().kind == "vendor":
        clauses.append("issuer_vendor_id=:v")
        params["v"] = principal.vendor_id
    if status:
        clauses.append("status=:st")
        params["st"] = status
    if price_match_flag:
        clauses.append("price_match_flag=:pmf")
        params["pmf"] = price_match_flag
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = session.execute(text(f"SELECT * FROM invoice {where} ORDER BY created_at DESC"), params).mappings().all()
    return {"items": [_invoice_out(dict(r)) for r in rows]}


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, principal: Principal = Depends(require_roles(*_INVOICE_READERS)),
                 session: Session = Depends(get_session, scope="function")):
    row = _invoice_or_404(session, invoice_id, principal)
    lines = session.execute(text("SELECT * FROM invoice_line WHERE invoice_id=:id"), {"id": invoice_id}).mappings().all()
    out = _invoice_out(dict(row))
    out["lines"] = [{**dict(l), "invoiced_price": _money(l["invoiced_price"])} for l in lines]
    return out


@router.post("/invoices/{invoice_id}/write-off", dependencies=[Depends(require_not_office_managed)])
def write_off_invoice(invoice_id: str, principal: Principal = Depends(require_roles(RoleCode.VENDOR_FINANCE)),
                       session: Session = Depends(get_session, scope="function")):
    row = _invoice_or_404(session, invoice_id, principal)
    if principal.vendor_id != row["issuer_vendor_id"]:
        raise ApiError("NOT_FOUND", "invoice not found")
    updated = MACHINES["SM-11"].apply(session, invoice_id, "WRITE_OFF", principal)
    return _invoice_out(dict(updated))


class PaymentAllocationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoice_id: str
    amount: Decimal


class PaymentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pharmacy_id: str
    vendor_id: str
    method: str
    amount: Decimal
    external_ref: str | None = None
    collected_by: str = "VENDOR_DIRECT"
    allocations: list[PaymentAllocationBody]


@router.post("/payments", dependencies=[Depends(require_not_office_managed)])
def create_payment(body: PaymentBody, principal: Principal = Depends(require_roles(*_PAYMENT_WRITERS)),
                    session: Session = Depends(get_session, scope="function")):
    """A10: `ledger_entry(PAYMENT, -allocated amount)` one entry per
    allocation; R-088 refuses `collected_by <> VENDOR_DIRECT`; R-090 (Σ
    allocations <= payment.amount) and R-091 (Σ per invoice <= outstanding)
    enforced here, inside the same transaction, with each invoice row locked
    FOR UPDATE."""
    if principal.vendor_id and principal.vendor_id != body.vendor_id:
        raise ApiError("FORBIDDEN", "vendor scope mismatch")
    if body.collected_by != "VENDOR_DIRECT":
        raise ApiError("VALIDATION_ERROR", "collected_by must be VENDOR_DIRECT in v1", rule="R-088")
    if body.method not in ("MOBILE_MONEY", "BANK_TRANSFER", "CASH_ON_DELIVERY"):
        raise ApiError("VALIDATION_ERROR", "invalid method")
    if body.amount <= 0:
        raise ApiError("VALIDATION_ERROR", "amount must be > 0")
    if not body.allocations:
        raise ApiError("VALIDATION_ERROR", "allocations must not be empty")
    total_alloc = sum((a.amount for a in body.allocations), Decimal("0"))
    if total_alloc > body.amount:
        raise ApiError("VALIDATION_ERROR", "sum of allocations exceeds payment amount", rule="R-090")

    payment_id = new_id("pay")
    session.execute(
        text(
            "INSERT INTO payment (id, pharmacy_id, vendor_id, method, collected_by, amount, external_ref, "
            "recorded_by_user_id) VALUES (:id, :p, :v, :m, :cb, :amt, :ref, :uid)"
        ),
        {"id": payment_id, "p": body.pharmacy_id, "v": body.vendor_id, "m": body.method,
         "cb": body.collected_by, "amt": body.amount, "ref": body.external_ref, "uid": principal.user_id},
    )

    facility = session.execute(
        text("SELECT id FROM credit_facility WHERE vendor_id=:v AND pharmacy_id=:p"),
        {"v": body.vendor_id, "p": body.pharmacy_id},
    ).mappings().first()

    for alloc in body.allocations:
        invoice = session.execute(
            text("SELECT * FROM invoice WHERE id=:id FOR UPDATE"), {"id": alloc.invoice_id}
        ).mappings().first()
        if invoice is None:
            raise ApiError("NOT_FOUND", "invoice not found")
        if invoice["issuer_vendor_id"] != body.vendor_id or invoice["pharmacy_id"] != body.pharmacy_id:
            raise ApiError("VALIDATION_ERROR", "invoice does not belong to this vendor/pharmacy pair")
        already_paid = session.execute(
            text("SELECT COALESCE(SUM(amount),0) FROM payment_allocation WHERE invoice_id=:id"),
            {"id": alloc.invoice_id},
        ).scalar()
        already_credited = session.execute(
            text(
                "SELECT COALESCE(SUM(amount),0) FROM credit_note WHERE order_id=:o"
            ),
            {"o": invoice["order_id"]},
        ).scalar()
        outstanding = invoice["total_amount"] - already_paid - already_credited
        if alloc.amount > outstanding:
            raise ApiError("VALIDATION_ERROR", "allocation exceeds invoice outstanding balance", rule="R-091")
        session.execute(
            text("INSERT INTO payment_allocation (id, payment_id, invoice_id, amount) VALUES (:id, :pay, :inv, :amt)"),
            {"id": new_id("pal"), "pay": payment_id, "inv": alloc.invoice_id, "amt": alloc.amount},
        )
        if facility:
            session.execute(
                text(
                    "INSERT INTO ledger_entry (id, credit_facility_id, entry_type, reference_type, reference_id, "
                    "amount) VALUES (:id, :f, 'PAYMENT', 'payment', :ref, :amt)"
                ),
                {"id": new_id("led"), "f": facility["id"], "ref": payment_id, "amt": -alloc.amount},
            )
        new_outstanding = outstanding - alloc.amount
        trigger = "ALLOCATE_FULL" if new_outstanding <= 0 else "ALLOCATE_PARTIAL"
        MACHINES["SM-11"].apply(session, alloc.invoice_id, trigger, "SYSTEM")

    return {
        "id": payment_id, "pharmacy_id": body.pharmacy_id, "vendor_id": body.vendor_id, "method": body.method,
        "amount": _money(body.amount), "external_ref": body.external_ref, "collected_by": body.collected_by,
        "allocations": [{"invoice_id": a.invoice_id, "amount": _money(a.amount)} for a in body.allocations],
    }


@router.get("/payments")
def list_payments(principal: Principal = Depends(require_roles(*_PAYMENT_READERS)),
                   session: Session = Depends(get_session, scope="function")):
    clauses, params = [], {}
    if principal.scope().kind == "pharmacy":
        clauses.append("pharmacy_id=:ph")
        params["ph"] = principal.pharmacy_id
    elif principal.scope().kind == "vendor":
        clauses.append("vendor_id=:v")
        params["v"] = principal.vendor_id
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = session.execute(text(f"SELECT * FROM payment {where} ORDER BY recorded_at DESC"), params).mappings().all()
    return {"items": [{**dict(r), "amount": _money(r["amount"])} for r in rows]}
