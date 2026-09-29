"""A9.2 / A15.4 rows 3-7 — auth endpoints (reduced set: login, me; refresh
and select-membership are implemented for the common single-membership
case; see manifest for what's left).

Every duration here is read from `config_parameter` through
`rova.config_params.service` (A5/A2.10) — never a Python constant:
`CFG-LOGIN-LOCKOUT-ATTEMPTS` (A-153), `CFG-LOGIN-LOCKOUT-MINUTES`
(a backend-introduced key, A-BE-02's 15-minute figure — no §16 row names a
lockout *duration* key, only the attempt count, so this one is registered
the same way `CFG-HEALTH-SCORE-FORMULA`/`CFG-ETA-VENDOR-VARIANCE-HOURS`
were: a real assumption-register value, not a guess), and session idle
timeout split by surface per A-89: `CFG-MOBILE-SESSION-IDLE-TIMEOUT-HOURS`
(PH/VN/CR, 12h) vs `CFG-SESSION-IDLE-TIMEOUT-MINUTES` (OP/VW, 30min)."""
from typing import Literal

import hashlib
from datetime import timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal, get_principal
from rova.auth.security import hash_password, issue_access_token, new_refresh_token, verify_password
from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.db import get_session
from rova.core.errors import ApiError
from rova.core.ids import new_id

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_DESKTOP_SURFACES = {"OP", "VW"}  # A-89: minutes-based idle timeout


def _session_idle_delta(session: Session, surface: str) -> timedelta:
    if surface in _DESKTOP_SURFACES:
        minutes = int(cfg.get(session, "CFG-SESSION-IDLE-TIMEOUT-MINUTES"))
        return timedelta(minutes=minutes)
    hours = int(cfg.get(session, "CFG-MOBILE-SESSION-IDLE-TIMEOUT-HOURS"))
    return timedelta(hours=hours)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str
    password: str
    surface: Literal["PH", "VN", "VW", "CR", "OP"]


@router.post("/login")
def login(body: LoginBody, session: Session = Depends(get_session, scope="function")):
    user = session.execute(
        text("SELECT * FROM app_user WHERE phone=:p"), {"p": body.phone}
    ).mappings().first()
    if user is None:
        raise ApiError("UNAUTHENTICATED", "invalid phone or password")
    if user["locked_until"] and user["locked_until"] > now():
        raise ApiError("UNAUTHENTICATED", "account temporarily locked")
    if not verify_password(body.password, user["password_hash"]):
        lockout_attempts = int(cfg.get(session, "CFG-LOGIN-LOCKOUT-ATTEMPTS"))
        lockout_minutes = int(cfg.get(session, "CFG-LOGIN-LOCKOUT-MINUTES"))
        failed = user["failed_login_count"] + 1
        locked_until = now() + timedelta(minutes=lockout_minutes) if failed >= lockout_attempts else None
        session.execute(
            text("UPDATE app_user SET failed_login_count=:f, locked_until=:l WHERE id=:id"),
            {"f": failed, "l": locked_until, "id": user["id"]},
        )
        if locked_until:
            session.execute(
                text(
                    "INSERT INTO audit_event (id, actor_user_id, actor_role, action_code, subject_type, subject_id, rule_ref) "
                    "VALUES (:id, :u, 'SYSTEM', 'LOGIN_LOCKOUT', 'app_user', :u, NULL)"
                ),
                {"id": new_id("aud"), "u": user["id"]},
            )
        # B fix 1a: get_session() rolls back the whole request transaction on
        # any exception (A2.9), which was silently discarding this very
        # UPDATE/INSERT along with the 401 it's about to raise — seven wrong
        # passwords never actually persisted past attempt one, so lockout
        # never engaged. The failed-attempt count and any resulting lock must
        # survive the 401 that follows, so it is committed here, in its own
        # transaction, before raising. get_session()'s subsequent rollback()
        # then has nothing pending left to discard.
        session.commit()
        raise ApiError("UNAUTHENTICATED", "invalid phone or password")

    session.execute(text("UPDATE app_user SET failed_login_count=0, locked_until=NULL WHERE id=:id"), {"id": user["id"]})

    memberships = session.execute(
        text("SELECT * FROM membership WHERE user_id=:u AND status='ACTIVE'"), {"u": user["id"]}
    ).mappings().all()

    selected = memberships[0] if len(memberships) == 1 else None
    return _open_session(session, user["id"], memberships, selected, body.surface)


def _open_session(session: Session, user_id: str, memberships, selected, surface: str) -> dict:
    """The one place a login becomes tokens: an `auth_session` row with the
    hashed refresh token, and a short access token. Shared by `login` and by
    the pharmacy sign-up (`onboarding/signup_router.py`), so a self-created
    account is signed in exactly the way a seeded one is."""
    org_id = selected["organisation_id"] if selected else None
    roles = list(selected["role_codes"]) if selected else []

    ses_id = new_id("ses")
    refresh = new_refresh_token()
    session.execute(
        text(
            "INSERT INTO auth_session (id, user_id, membership_id, surface, refresh_token_hash, expires_at) "
            "VALUES (:id, :u, :m, :s, :h, :exp)"
        ),
        {
            "id": ses_id, "u": user_id, "m": selected["id"] if selected else None, "s": surface,
            "h": hashlib.sha256(refresh.encode()).hexdigest(),
            "exp": now() + _session_idle_delta(session, surface),
        },
    )

    access = issue_access_token(
        user_id=user_id, membership_id=selected["id"] if selected else None, organisation_id=org_id,
        roles=roles, surface=surface, session_id=ses_id,
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "memberships": [
            {"id": m["id"], "organisation_id": m["organisation_id"], "roles": list(m["role_codes"])}
            for m in memberships
        ],
        "selected_membership_id": selected["id"] if selected else None,
    }


class SelectMembershipBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    membership_id: str


@router.post("/select-membership")
def select_membership(body: SelectMembershipBody, principal: Principal = Depends(get_principal),
                       session: Session = Depends(get_session, scope="function")):
    m = session.execute(
        text("SELECT * FROM membership WHERE id=:m AND user_id=:u AND status='ACTIVE'"),
        {"m": body.membership_id, "u": principal.user_id},
    ).mappings().first()
    if m is None:
        raise ApiError("NOT_FOUND", "membership not found")
    ses_id = new_id("ses")
    refresh = new_refresh_token()
    session.execute(
        text(
            "INSERT INTO auth_session (id, user_id, membership_id, surface, refresh_token_hash, expires_at) "
            "VALUES (:id, :u, :m, :s, :h, :exp)"
        ),
        {"id": ses_id, "u": principal.user_id, "m": m["id"], "s": principal.surface,
         "h": hashlib.sha256(refresh.encode()).hexdigest(), "exp": now() + _session_idle_delta(session, principal.surface)},
    )
    access = issue_access_token(
        user_id=principal.user_id, membership_id=m["id"], organisation_id=m["organisation_id"],
        roles=list(m["role_codes"]), surface=principal.surface, session_id=ses_id,
    )
    return {"access_token": access, "refresh_token": refresh}


@router.post("/logout")
def logout(principal: Principal = Depends(get_principal), session: Session = Depends(get_session, scope="function")):
    session.execute(
        text("UPDATE auth_session SET revoked_at=:n WHERE user_id=:u AND revoked_at IS NULL"),
        {"n": now(), "u": principal.user_id},
    )
    return {"status": "ok"}


@router.get("/me")
def me(principal: Principal = Depends(get_principal), session: Session = Depends(get_session, scope="function")):
    primary_interface = session.execute(
        text("SELECT current_value FROM mode_switch WHERE switch_key='PRIMARY_INTERFACE' AND scope_type='GLOBAL'")
    ).scalar()
    active_vendor_count = session.execute(text("SELECT count(*) FROM vendor_account WHERE status='ACTIVE'")).scalar()
    # The client decides from this whether to show the "being checked"
    # banner and whether the cart button can order (signup spec §2.2).
    pharmacy_status = session.execute(
        text("SELECT status FROM pharmacy_account WHERE id=:p"), {"p": principal.pharmacy_id}
    ).scalar() if principal.pharmacy_id else None
    return {
        "user_id": principal.user_id,
        "membership_id": principal.membership_id,
        "organisation_id": principal.organisation_id,
        "roles": sorted(principal.roles),
        "surface": principal.surface,
        "pharmacy_id": principal.pharmacy_id,
        "pharmacy_status": pharmacy_status,
        "vendor_id": principal.vendor_id,
        "primary_interface": primary_interface,
        "active_vendor_count": active_vendor_count,
    }
