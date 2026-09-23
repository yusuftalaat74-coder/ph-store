"""SLA timer helpers (E-044, A4.2's closing paragraph: "every SlaTimer
started in a side-effect column is cancelled or expired by name"). Used as
`Transition.effects` in the machines under `rova/domain/machines/` — never
called from a router. `start` enforces `uq_sla_timer_running` itself by
cancelling any existing RUNNING timer for the same
`(policy_type, subject_type, subject_id)` key first, so a re-entrant or
retried transition never trips the unique index.

Durations come from `config_parameter` via `rova.config_params.service.get`
(A5), never a Python constant — the whole point of A14.6 being able to
change the SLA hours per vendor/region without a deploy.
"""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.config_params import service as cfg
from rova.core.clock import now
from rova.core.ids import new_id

_UNIT_TO_HOURS = {"HOURS": Decimal(1), "DAYS": Decimal(24), "MINUTES": Decimal(1) / Decimal(60)}


def start(
    session: Session,
    *,
    policy_type: str,
    subject_type: str,
    subject_id: str,
    cfg_key: str,
    unit: str = "HOURS",
    vendor_id: str | None = None,
    region_code: str | None = None,
) -> str:
    """Cancels any RUNNING timer for the same key, then starts a new one.
    Returns the new sla_timer.id."""
    cancel(session, policy_type=policy_type, subject_type=subject_type, subject_id=subject_id)
    raw = cfg.get(session, cfg_key, vendor_id=vendor_id, region_code=region_code)
    hours = Decimal(raw) * _UNIT_TO_HOURS[unit]
    timer_id = new_id("sla")
    session.execute(
        text(
            "INSERT INTO sla_timer (id, policy_type, subject_type, subject_id, expires_at, config_source) "
            "VALUES (:id, :pt, :st, :sid, :exp, :src)"
        ),
        {
            "id": timer_id, "pt": policy_type, "st": subject_type, "sid": subject_id,
            "exp": now() + timedelta(hours=float(hours)), "src": cfg_key,
        },
    )
    return timer_id


def cancel(session: Session, *, policy_type: str, subject_type: str, subject_id: str) -> None:
    session.execute(
        text(
            "UPDATE sla_timer SET status='CANCELLED', updated_at=:n WHERE policy_type=:pt AND subject_type=:st "
            "AND subject_id=:sid AND status='RUNNING'"
        ),
        {"n": now(), "pt": policy_type, "st": subject_type, "sid": subject_id},
    )


def is_running(session: Session, *, policy_type: str, subject_type: str, subject_id: str) -> bool:
    return session.execute(
        text(
            "SELECT 1 FROM sla_timer WHERE policy_type=:pt AND subject_type=:st AND subject_id=:sid "
            "AND status='RUNNING' AND expires_at > :now"
        ),
        {"pt": policy_type, "st": subject_type, "sid": subject_id, "now": now()},
    ).first() is not None
