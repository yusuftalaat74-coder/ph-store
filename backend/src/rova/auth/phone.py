"""Mozambican mobile numbers, the way a pharmacist types them.

A number arrives as "84 123 4567", "841234567", "258 84 123 4567",
"00258841234567" or "+258 84-123-4567"; the database stores one spelling,
E.164 without spaces (`+258841234567`), which is what the seeded accounts
already use and what `login` matches exactly.

Only mobile ranges are accepted (82–87): the account is the pharmacist's
phone, and a landline cannot receive the call the reviewer makes before
approving (signup spec §3, D-1).

The UI applies the same rules client-side before sending a login, so the
server's `login` can keep its exact match (D-11).
"""
import re

_VALID = re.compile(r"^\+2588[2-7]\d{7}$")


class InvalidPhone(ValueError):
    """The input does not normalise to a Mozambican mobile number."""


def normalise_mz_phone(raw: str) -> str:
    s = (raw or "").strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not plus and digits.startswith("00"):
        digits, plus = digits[2:], True
    if plus:
        candidate = "+" + digits
    elif len(digits) == 12 and digits.startswith("258"):
        candidate = "+" + digits
    elif len(digits) == 9 and digits.startswith("8"):
        candidate = "+258" + digits
    else:
        candidate = "+" + digits if digits else ""
    if not _VALID.match(candidate):
        raise InvalidPhone(raw)
    return candidate
