"""Signup spec §2.1 — a Mozambican mobile number, the way it is typed."""
import pytest

from rova.auth.phone import InvalidPhone, normalise_mz_phone


@pytest.mark.parametrize("raw, expected", [
    ("84 123 4567", "+258841234567"),
    ("841234567", "+258841234567"),
    ("00258841234567", "+258841234567"),
    ("0025884 123 4567", "+258841234567"),
    ("258 84 123 4567", "+258841234567"),
    ("+258 87-123-4567", "+258871234567"),
    ("+258841234567", "+258841234567"),
    ("  82.123.4567 ", "+258821234567"),
])
def test_normalises(raw, expected):
    assert normalise_mz_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    "71234567",            # too short, not a mobile prefix
    "+2588412345678",      # one digit too many
    "84 000 000",          # 8 digits
    "88 123 4567",         # 88 is not a mobile range
    "21 123 456",          # Maputo landline
    "+27 82 123 4567",     # South Africa
    "",
    "abc",
])
def test_refuses(raw):
    with pytest.raises(InvalidPhone):
        normalise_mz_phone(raw)
