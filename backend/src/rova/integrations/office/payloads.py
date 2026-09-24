"""Payload builders for Store -> Office events (read only; SPEC 5.5). Every
payload is self-contained: Office never reads the Store database."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.core.money import money_str


def _iso(v):
    return v.isoformat() if v is not None else None


def pharmacy_block(session: Session, pharmacy_id: str, vendor_id: str | None = None) -> dict:
    p = session.execute(
        text("SELECT p.*, o.tax_id, o.legal_name FROM pharmacy_account p JOIN organisation o ON o.id = p.organisation_id "
             "WHERE p.id=:p"), {"p": pharmacy_id},
    ).mappings().one()
    phone = session.execute(
        text("SELECT u.phone FROM membership m JOIN app_user u ON u.id = m.user_id WHERE m.organisation_id=:o "
             "AND m.status='ACTIVE' ORDER BY m.created_at LIMIT 1"), {"o": p["organisation_id"]},
    ).scalar()
    out = {"id": p["id"], "name": p["trade_name"], "legal_name": p["legal_name"], "nuit": p["tax_id"],
           "phone": phone or p["assisted_channel_ref"], "address": p["address"], "status": p["status"],
           "region_code": p["region_code"]}
    if vendor_id:
        f = session.execute(text("SELECT id, limit_amount, terms_days, status, opening_balance FROM credit_facility "
                                 "WHERE vendor_id=:v AND pharmacy_id=:p"), {"v": vendor_id, "p": pharmacy_id}).mappings().first()
        out["credit_facility_id"] = f["id"] if f else None
        if f:
            out["credit_facility"] = {"id": f["id"], "limit_amount": money_str(f["limit_amount"]),
                                      "terms_days": f["terms_days"], "status": f["status"],
                                      "opening_balance": money_str(f["opening_balance"])}
    return out


def build_order_created(session: Session, order_id: str) -> dict:
    o = session.execute(text('SELECT * FROM "order" WHERE id=:o'), {"o": order_id}).mappings().one()
    lines = session.execute(
        text("SELECT ol.*, ip.inn, ip.form, ip.strength, ip.pack_size FROM order_line ol "
             "JOIN index_product ip ON ip.id = ol.index_product_id WHERE ol.order_id=:o ORDER BY ol.created_at, ol.id"),
        {"o": order_id},
    ).mappings().all()
    return {
        "order": {"id": o["id"], "number": o["number"], "request_id": o["request_id"], "vendor_id": o["vendor_id"],
                  "pharmacy_id": o["pharmacy_id"], "status": o["status"], "payment_terms": o["payment_terms"],
                  "credit_days": o["credit_days"], "delivery_mode": o["delivery_mode"],
                  "goods_total": money_str(o["goods_total"]), "created_at": _iso(o["created_at"])},
        "pharmacy": pharmacy_block(session, o["pharmacy_id"], o["vendor_id"]),
        "lines": [{"id": l["id"], "request_line_id": l["request_line_id"], "index_product_id": l["index_product_id"],
                   "product": {"name_pt": " ".join(str(x) for x in (l["inn"], l["strength"], l["form"]) if x),
                               "regulated_price": l["regulated_price"], "pack_size": l["pack_size"]},
                   "ordered_qty": l["ordered_qty"], "unit_price": money_str(l["unit_price"]),
                   "price_source": l["price_source"]} for l in lines],
    }


def build_order_cancelled(session: Session, order_id: str) -> dict:
    o = session.execute(text('SELECT id, number, cancel_reason FROM "order" WHERE id=:o'), {"o": order_id}).mappings().one()
    return {"order_id": o["id"], "number": o["number"], "reason": o["cancel_reason"]}


def build_receipt(session: Session, order_id: str) -> dict:
    rows = session.execute(
        text("SELECT rl.order_line_id, rl.accepted_qty, rl.rejected_qty, rl.reason_code FROM receipt r "
             "JOIN receipt_line rl ON rl.receipt_id = r.id WHERE r.order_id=:o ORDER BY rl.created_at"), {"o": order_id},
    ).mappings().all()
    return {"order_id": order_id,
            "lines": [{"store_order_line_id": r["order_line_id"], "accepted_qty": r["accepted_qty"],
                       "rejected_qty": r["rejected_qty"], "reason_code": r["reason_code"]} for r in rows]}


def build_disputed(session: Session, order_id: str) -> dict:
    d = session.execute(text("SELECT id, type FROM dispute WHERE order_id=:o ORDER BY created_at DESC LIMIT 1"),
                        {"o": order_id}).mappings().first()
    return {"order_id": order_id, "dispute_id": d["id"] if d else None, "type": d["type"] if d else None,
            **{k: v for k, v in build_receipt(session, order_id).items() if k == "lines"}}


def build_dispute_resolved(session: Session, dispute_id: str) -> dict:
    d = session.execute(text("SELECT id, order_id, outcome, type FROM dispute WHERE id=:d"), {"d": dispute_id}).mappings().one()
    return {"order_id": d["order_id"], "dispute_id": d["id"], "outcome": d["outcome"], "type": d["type"],
            **{k: v for k, v in build_receipt(session, d["order_id"]).items() if k == "lines"}}


def build_return(session: Session, return_id: str) -> dict:
    r = session.execute(text('SELECT * FROM "return" WHERE id=:r'), {"r": return_id}).mappings().one()
    lines = session.execute(text("SELECT order_line_id, qty, reason_code FROM return_line WHERE return_id=:r"),
                            {"r": return_id}).mappings().all()
    return {"return_id": r["id"], "order_id": r["order_id"], "rma_number": r["rma_number"], "status": r["status"],
            "origin": r["origin"], "lines": [{"store_order_line_id": l["order_line_id"], "qty": l["qty"],
                                              "reason_code": l["reason_code"]} for l in lines]}


def build_pharmacy(session: Session, pharmacy_id: str) -> dict:
    p = pharmacy_block(session, pharmacy_id)
    row = session.execute(text("SELECT suspension_cause FROM pharmacy_account WHERE id=:p"), {"p": pharmacy_id}).mappings().one()
    return {**p, "pharmacy_id": pharmacy_id, "suspension_cause": row["suspension_cause"]}


def build_fee(session: Session, fee_event_id: str) -> dict:
    f = session.execute(
        text("SELECT fe.*, fs.type AS fee_type, fs.payer, o.type AS org_type, o.legal_name, "
             "va.id AS payer_vendor_id, pa.id AS payer_pharmacy_id, ord.number AS order_number, "
             "ord.vendor_id AS order_vendor_id, ord.pharmacy_id AS order_pharmacy_id "
             "FROM fee_event fe JOIN fee_schedule fs ON fs.id = fe.fee_schedule_id "
             "JOIN organisation o ON o.id = fe.payer_org_id "
             "LEFT JOIN vendor_account va ON va.organisation_id = o.id "
             "LEFT JOIN pharmacy_account pa ON pa.organisation_id = o.id "
             'LEFT JOIN "order" ord ON ord.id = fe.order_id WHERE fe.id=:f'), {"f": fee_event_id},
    ).mappings().one()
    return {"fee_event_id": f["id"], "fee_type": f["fee_type"], "payer_org_id": f["payer_org_id"],
            "payer_type": f["org_type"], "payer_name": f["legal_name"], "payer_vendor_id": f["payer_vendor_id"],
            "payer_pharmacy_id": f["payer_pharmacy_id"], "amount": money_str(f["amount"]),
            "base_amount": money_str(f["base_amount"]), "status": f["status"], "order_id": f["order_id"],
            "order_number": f["order_number"], "vendor_id": f["order_vendor_id"], "pharmacy_id": f["order_pharmacy_id"],
            "occurred_at": _iso(f["occurred_at"]), "reason": f["reversal_reason"]}


def build_platform_invoice(session: Session, platform_invoice_id: str) -> dict:
    p = session.execute(
        text("SELECT pi.*, o.type AS org_type, va.id AS payer_vendor_id, pa.id AS payer_pharmacy_id "
             "FROM platform_invoice pi JOIN organisation o ON o.id = pi.payer_org_id "
             "LEFT JOIN vendor_account va ON va.organisation_id = o.id "
             "LEFT JOIN pharmacy_account pa ON pa.organisation_id = o.id WHERE pi.id=:p"), {"p": platform_invoice_id},
    ).mappings().one()
    return {"platform_invoice_id": p["id"], "number": p["number"], "payer_org_id": p["payer_org_id"],
            "payer_type": p["org_type"], "payer_vendor_id": p["payer_vendor_id"],
            "payer_pharmacy_id": p["payer_pharmacy_id"], "amount": money_str(p["amount"]),
            "period": {"start": _iso(p["period_start"]), "end": _iso(p["period_end"])}, "status": p["status"]}


def build_credit_facility(session: Session, facility_id: str) -> dict:
    f = session.execute(text("SELECT cf.*, p.trade_name FROM credit_facility cf JOIN pharmacy_account p "
                             "ON p.id = cf.pharmacy_id WHERE cf.id=:f"), {"f": facility_id}).mappings().one()
    return {"id": f["id"], "credit_facility_id": f["id"], "vendor_id": f["vendor_id"], "pharmacy_id": f["pharmacy_id"],
            "pharmacy_name": f["trade_name"], "limit_amount": money_str(f["limit_amount"]), "terms_days": f["terms_days"],
            "opening_balance": money_str(f["opening_balance"]), "status": f["status"]}
