"""The cart endpoints.

Every one of them returns the whole cart. The phone never merges — it
replaces what it is holding with what came back — so two people in the same
pharmacy editing the same cart converge on the next tap instead of drifting
apart. That is worth the extra rows on the wire: a cart is a few lines, and
a wrong cart that looks right is the failure that costs money.
"""
from decimal import Decimal

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
    # The price printed on the card that was tapped. It becomes the claim the
    # invoice is checked against, so the pharmacist is held to the number he
    # actually saw and not to one computed a moment later on the server.
    #
    # Two decimals, because the column is NUMERIC(14,2): without this a claim
    # of "120.005" would be rounded on the way in and then read back as a
    # price that had moved, against a price that had not.
    price_seen: Decimal | None = Field(default=None, ge=0, decimal_places=2)


class QtyBody(BaseModel):
    """A quantity on its own: changing which product a line points at is a
    different act, and `/resolve` on the intake side is where that lives."""
    model_config = ConfigDict(extra="forbid")
    qty_requested: int = Field(ge=1, le=100000)


class CartLinesBody(BaseModel):
    """One line, or a batch — "add this order again" is one call."""
    model_config = ConfigDict(extra="forbid")
    # Bounded: each line is a full ranking, and an unbounded list would let
    # one request hold a connection for as long as it liked.
    lines: list[CartLine] = Field(min_length=1, max_length=200)


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
    for line in body.lines:
        set_line(session, pharmacy_id=pharmacy_id, user_id=principal.user_id,
                 index_product_id=line.index_product_id, qty=line.qty_requested,
                 price_shown=line.price_seen)
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
