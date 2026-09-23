"""Password hashing (argon2id) and JWT issuing/verification (A9.2)."""
import secrets
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from rova.config import get_settings
from rova.core.clock import now

_hasher = PasswordHasher()

ACCESS_TOKEN_MINUTES = 10  # [assume: A-BE-02]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def issue_access_token(*, user_id: str, membership_id: str | None, organisation_id: str | None,
                        roles: list[str], surface: str, session_id: str) -> str:
    payload = {
        "sub": user_id,
        "mid": membership_id,
        "org": organisation_id,
        "roles": roles,
        "surface": surface,
        "sid": session_id,
        "exp": now() + timedelta(minutes=ACCESS_TOKEN_MINUTES),
        "iat": now(),
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
