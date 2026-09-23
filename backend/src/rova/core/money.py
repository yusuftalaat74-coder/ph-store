"""A2.4 — money is always decimal.Decimal, NUMERIC(14,2), ROUND_HALF_UP, once."""
from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")


def quantize(value: Decimal) -> Decimal:
    """Round to 2dp, ROUND_HALF_UP, applied once (A2.4)."""
    if not isinstance(value, Decimal):
        raise TypeError("money values must be decimal.Decimal, never float")
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def money_str(value: Decimal) -> str:
    """JSON serialisation of money: a string, never a number (A2.4)."""
    return str(quantize(value))


def zero() -> Decimal:
    return Decimal("0.00")
