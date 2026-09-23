"""A9.3 — Principal and RBAC. `get_principal` re-reads membership.status on
every request so a revoked membership fails 401 on its very next request,
not only at refresh (A9.2, B3.23)."""
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.security import decode_access_token
from rova.core.db import Scope, get_session
from rova.core.errors import ApiError


@dataclass(frozen=True)
class Principal:
    user_id: str
    membership_id: str | None
    organisation_id: str | None
    roles: frozenset[str]
    surface: str
    pharmacy_id: str | None
    vendor_id: str | None
    transporter_id: str | None

    def scope(self) -> Scope:
        if self.pharmacy_id:
            return Scope.pharmacy(self.pharmacy_id)
        if self.vendor_id:
            return Scope.vendor(self.vendor_id)
        return Scope.platform()


def get_principal(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session, scope="function"),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError("UNAUTHENTICATED", "missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        claims = decode_access_token(token)
    except jwt.PyJWTError:
        raise ApiError("UNAUTHENTICATED", "invalid or expired token")

    membership_id = claims.get("mid")
    organisation_id = claims.get("org")
    pharmacy_id = vendor_id = transporter_id = None

    if membership_id:
        row = session.execute(
            text("SELECT status, role_codes FROM membership WHERE id=:m"), {"m": membership_id}
        ).mappings().first()
        if row is None or row["status"] != "ACTIVE":
            raise ApiError("UNAUTHENTICATED", "membership revoked")
        roles = frozenset(row["role_codes"])
    else:
        roles = frozenset(claims.get("roles", []))

    if organisation_id:
        org_type = session.execute(
            text("SELECT type FROM organisation WHERE id=:o"), {"o": organisation_id}
        ).scalar()
        if org_type == "PHARMACY":
            pharmacy_id = session.execute(
                text("SELECT id FROM pharmacy_account WHERE organisation_id=:o"), {"o": organisation_id}
            ).scalar()
        elif org_type == "VENDOR":
            vendor_id = session.execute(
                text("SELECT id FROM vendor_account WHERE organisation_id=:o"), {"o": organisation_id}
            ).scalar()
        elif org_type == "TRANSPORTER":
            transporter_id = session.execute(
                text("SELECT id FROM transporter_account WHERE organisation_id=:o"), {"o": organisation_id}
            ).scalar()

    return Principal(
        user_id=claims["sub"], membership_id=membership_id, organisation_id=organisation_id,
        roles=roles, surface=claims.get("surface", ""),
        pharmacy_id=pharmacy_id, vendor_id=vendor_id, transporter_id=transporter_id,
    )


def require_roles(*roles: str):
    allowed = frozenset(roles)

    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not (principal.roles & allowed):
            raise ApiError("FORBIDDEN", "role not permitted for this endpoint")
        return principal

    return _dep
