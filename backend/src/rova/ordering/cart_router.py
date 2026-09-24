"""The cart endpoints.

Every one of them returns the whole cart. The phone never merges — it
replaces what it is holding with what came back — so two people in the same
pharmacy editing the same cart converge on the next tap instead of drifting
apart. That is worth the extra rows on the wire: a cart is a few lines, and
a wrong cart that looks right is the failure that costs money.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, require_roles
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.domain.enums import RoleCode
from rova.ordering.cart import read_cart, set_line, set_qty

router = APIRouter(prefix="/v1", tags=["cart"])

_PHARMACY = (RoleCode.PHARMACY_ADMIN, RoleCode.PHARMACY_BUYER)


class CartLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index_product_id: str
    # A set, not an increment: the phone sends the number it is showing, so a
    # retry after a dropped connection lands on the same quantity instead of
    # doubling it.
    qty_requested: int = Field(ge=1, le=100000)


class QtyBody(BaseModel):
    """A quantity on its own: changing which product a line points at is a
    different act, and `/resolve` on the intake side is where that lives."""
    model_config = ConfigDict(extra="forbid")
    qty_requested: int = Field(ge=1, le=100000)


class CartLinesBody(BaseModel):
    """One line, or a batch — "add this order again" is one call."""
    model_config = ConfigDict(extra="forbid")
    lines: list[CartLine]


def _pharmacy(principal: Principal) -> str:
    if principal.pharmacy_id is None:
        raise ApiError("FORBIDDEN", "principal is not a pharmacy member")
    return principal.pharmacy_id


@router.get("/cart")
def get_cart(principal: Principal = Depends(require_roles(*_PHARMACY)),
             session: Session = Depends(get_session, scope="function")):
    return read_cart(session, _pharmacy(principal))


@router.post("/cart/lines")
def put_cart_lines(body: CartLinesBody,
                   principal: Principal = Depends(require_roles(*_PHARMACY)),
                   session: Session = Depends(get_session, scope="function")):
    pharmacy_id = _pharmacy(principal)
    if not body.lines:
        raise ApiError("VALIDATION_ERROR", "lines must not be empty")
    for line in body.lines:
        set_line(session, pharmacy_id=pharmacy_id, user_id=principal.user_id,
                 index_product_id=line.index_product_id, qty=line.qty_requested)
    return read_cart(session, pharmacy_id)


@router.patch("/request-lines/{line_id}")
def patch_line(line_id: str, body: QtyBody,
               principal: Principal = Depends(require_roles(*_PHARMACY)),
               session: Session = Depends(get_session, scope="function")):
    """Only the quantity moves. Changing which product a line points at is a
    different act — that is what `/resolve` is for on the intake side."""
    pharmacy_id = _pharmacy(principal)
    set_qty(session, line_id=line_id, pharmacy_id=pharmacy_id, qty=body.qty_requested)
    return read_cart(session, pharmacy_id)
