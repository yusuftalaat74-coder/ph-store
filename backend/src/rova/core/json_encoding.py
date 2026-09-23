"""A2.4 — 'money is always decimal.Decimal ... never a float', including on
the wire. `rova.core.money.money_str()` already does this correctly for the
few call sites that use it explicitly, but every route in this backend
returns raw `dict(row)`/`dict(mapping)` objects straight from SQLAlchemy
`Row.mappings()`, which still hold `decimal.Decimal` values for every
NUMERIC column — money and otherwise. FastAPI serialises those through
`fastapi.encoders.jsonable_encoder`, which has its own opinion, independent
of `money_str`: `ENCODERS_BY_TYPE[Decimal]` is `decimal_encoder`, which
returns a JSON **number** (`int` when the exponent is >= 0, else `float`) —
so `Decimal("850.00")` becomes the JSON number `850.0`, silently, on every
endpoint, with no per-endpoint code visibly at fault (B fix 1b).

`jsonable_encoder` has no supported hook to change the behaviour for one
type globally (its `custom_encoder` param is per-call, not per-app), so the
one clean fix is the one FastAPI's own maintainers document for exactly
this situation: mutate `ENCODERS_BY_TYPE` in place. It is looked up by
`type(obj) in ENCODERS_BY_TYPE` at encode time (not through the derived,
frozen-at-import `encoders_by_class_tuples` cache), so a mutation of the
same dict object takes effect for every request from that point on,
regardless of which router or module produced the value — no router file
needs to change. This is deliberately blunt: it stringifies every Decimal
on the wire, money or not (`vendor_score`, `confidence`, `latitude`, a
`fee_schedule.rate_or_amount`, ...), which is strictly safer than picking
money fields by name and risking missing one, and it matches money_str's
own contract (a string, never a bare JSON number) for the fields that do
carry money."""
from decimal import Decimal

from fastapi.encoders import ENCODERS_BY_TYPE


def install_decimal_string_encoder() -> None:
    """Idempotent — safe to call more than once per process (mirrors
    rova.domain.hooks.wire()'s own idempotency contract)."""
    ENCODERS_BY_TYPE[Decimal] = str
