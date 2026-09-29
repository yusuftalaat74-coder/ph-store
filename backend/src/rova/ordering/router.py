"""A15.4 rows 92-100 (reduced set: create request, add line, checkout)."""
from typing import Literal

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.core.db import Scope, get_session
from rova.core.errors import ApiError
from rova.core.ids import new_id
from rova.core.idempotency import check_and_store, store
from rova.domain.enums import RoleCode
from rova.ordering.checkout import allocate_remainder as run_allocate_remainder
from rova.ordering.checkout import checkout as run_checkout

router = APIRouter(prefix="/v1", tags=["ordering"])

_PHARMACY_ROLES = (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER, RoleCode.OPS_REVIEWER)


class CreateRequestBody(BaseModel):
    """`mode` and `allocation_strategy` are `Literal`s, not free strings.

    Before this, a wrong value passed validation, reached the DDL CHECK and
    came back as an opaque `500 INTERNAL` with no field named — found during
    the September 2026 verification run against the live server. The value
    lists are copied verbatim from `request_mode_check` and
    `request_allocation_strategy_check` in `migrations/sql/0001_initial.sql`;
    `tests/test_enums_match_ddl.py` is what keeps them honest.
    """
    model_config = ConfigDict(extra="forbid")
    mode: Literal["CATALOGUE", "RFQ", "ASSISTED"] = "CATALOGUE"
    allocation_strategy: Literal["FEWEST_VENDORS", "FASTEST_DISPATCH", "BEST_TERMS"] = "FEWEST_VENDORS"


@router.post("/requests")
def create_request(body: CreateRequestBody, principal: Principal = Depends(require_roles(*_PHARMACY_ROLES)),
                    session: Session = Depends(get_session, scope="function")):
    if principal.pharmacy_id is None:
        raise ApiError("FORBIDDEN", "principal is not a pharmacy member")
    number = "RQ-" + str(__import__("datetime").date.today().year) + "-" + str(
        session.execute(text("SELECT nextval('request_number_seq')")).scalar()
    ).zfill(6)
    rid = new_id("req")
    session.execute(
        text(
            "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy, "
            "created_by_user_id) VALUES (:id, :num, :ph, :mode, 'APP', 'DRAFT', :strat, :uid)"
        ),
        {"id": rid, "num": number, "ph": principal.pharmacy_id, "mode": body.mode,
         "strat": body.allocation_strategy, "uid": principal.user_id},
    )
    return {"id": rid, "number": number, "status": "DRAFT"}


@router.get("/requests/{request_id}")
def get_request(request_id: str, principal: Principal = Depends(require_roles(*_PHARMACY_ROLES)),
                 session: Session = Depends(get_session, scope="function")):
    row = session.execute(text("SELECT * FROM request WHERE id=:r"), {"r": request_id}).mappings().first()
    if row is None or not Scope.pharmacy(row["pharmacy_id"]).owns_pharmacy(principal.pharmacy_id or ""):
        raise ApiError("NOT_FOUND", "request not found")
    # The product's name rides along with each line: the order screen is
    # where the pharmacist follows what he bought, and `idx_…` is not a
    # medicine to him. LEFT JOIN — a WhatsApp line nobody resolved has none.
    lines = session.execute(
        text("SELECT rl.*, p.brand_name, p.inn, p.strength, p.form "
             "FROM request_line rl LEFT JOIN index_product p ON p.id = rl.index_product_id "
             "WHERE rl.request_id=:r ORDER BY rl.created_at, rl.id"),
        {"r": request_id},
    ).mappings().all()
    orders = session.execute(text('SELECT id, number, vendor_id, status, goods_total FROM "order" WHERE request_id=:r'),
                              {"r": request_id}).mappings().all()
    return {"id": row["id"], "number": row["number"], "status": row["status"], "mode": row["mode"],
            "lines": [dict(l) for l in lines], "orders": [dict(o) for o in orders]}


class AddLineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index_product_id: str
    qty_requested: int


@router.post("/requests/{request_id}/lines")
def add_line(request_id: str, body: AddLineBody, principal: Principal = Depends(require_roles(*_PHARMACY_ROLES)),
             session: Session = Depends(get_session, scope="function")):
    req = session.execute(text("SELECT * FROM request WHERE id=:r"), {"r": request_id}).mappings().first()
    if req is None or req["pharmacy_id"] != principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "request not found")
    if req["status"] != "DRAFT":
        raise ApiError("GUARD_FAILED", "request is not in DRAFT")
    if body.qty_requested <= 0:
        raise ApiError("VALIDATION_ERROR", "qty_requested must be > 0")
    line_id = new_id("rql")
    session.execute(
        text(
            "INSERT INTO request_line (id, request_id, index_product_id, qty_requested, match_status) "
            "VALUES (:id, :r, :p, :q, 'RESOLVED')"
        ),
        {"id": line_id, "r": request_id, "p": body.index_product_id, "q": body.qty_requested},
    )
    return {"id": line_id, "index_product_id": body.index_product_id, "qty_requested": body.qty_requested}


class CheckoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # J-20 minimum (item 3, backend-review-r1.md): {vendor_id: "UPFRONT"} forces
    # that vendor's sub-basket onto UPFRONT terms, bypassing a blocked credit gate.
    payment_overrides: dict[str, str] = {}
    # What the screen is showing right now, line id -> price as a 2dp string.
    # Optional, and it does not weaken the guard to leave it out: a line's
    # stored `price_seen` is already a claim, and checkout refuses when the
    # figure about to be billed does not match it. Sending this replaces that
    # claim with a newer one, which is what the "continue at the new prices"
    # button does after a refusal. A line that never displayed a price -- the
    # WhatsApp lane -- claims nothing and is not policed.
    acknowledged_prices: dict[str, str] | None = None


@router.post("/requests/{request_id}/checkout")
def checkout_endpoint(
    request_id: str,
    response: Response,
    body: CheckoutBody = CheckoutBody(),
    principal: Principal = Depends(require_roles(*_PHARMACY_ROLES)),
    session: Session = Depends(get_session, scope="function"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    req = session.execute(text("SELECT pharmacy_id FROM request WHERE id=:r"), {"r": request_id}).mappings().first()
    if req is None or req["pharmacy_id"] != principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "request not found")

    body_dict = body.model_dump()
    principal_id = principal.membership_id or principal.user_id
    replay = check_and_store(
        session, key=idempotency_key, principal_id=principal_id, method="POST",
        path=f"/v1/requests/{request_id}/checkout", body=body_dict, response=response,
    )
    if replay is not None:
        return replay["body"]

    result = run_checkout(session, request_id=request_id, actor=principal,
                          payment_overrides=body.payment_overrides,
                          acknowledged_prices=body.acknowledged_prices)
    store(session, key=idempotency_key, principal_id=principal_id, method="POST",
          path=f"/v1/requests/{request_id}/checkout", body=body_dict, status_code=200, response_body=result)
    return result


@router.post("/request-lines/{line_id}/allocate")
def allocate_remainder_line(line_id: str, principal: Principal = Depends(require_roles(*_PHARMACY_ROLES)),
                             session: Session = Depends(get_session, scope="function")):
    """Defect 4 fix: the actual re-entry into ranking/allocation for a
    rerouted remainder `request_line` (see `rova/ordering/checkout.py::
    allocate_remainder` for why `checkout()` itself cannot be re-run here)."""
    row = session.execute(
        text("SELECT rl.request_id, r.pharmacy_id FROM request_line rl JOIN request r ON r.id = rl.request_id "
             "WHERE rl.id=:id"),
        {"id": line_id},
    ).mappings().first()
    if row is None or row["pharmacy_id"] != principal.pharmacy_id:
        raise ApiError("NOT_FOUND", "request line not found")
    return run_allocate_remainder(session, request_line_id=line_id, actor=principal)
