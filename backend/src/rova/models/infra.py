"""SQLAlchemy Core Table objects, generated from migrations/sql/0001_initial.sql.

These carry column names, Python-side types (Numeric precision, JSONB, ARRAY,
timezone-aware DateTime) and primary keys only — every CHECK, FOREIGN KEY,
UNIQUE index and DEFAULT lives in the DDL (A3, applied by Alembic) and is not
re-declared here, so this module can never drift into re-creating the schema.
`metadata.create_all()` is never called in this codebase."""
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Integer, Numeric, Table, Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from rova.models.meta import metadata


region_table = Table(
    "region",
    metadata,
    Column("code", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("transit_window_hours", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

config_parameter_table = Table(
    "config_parameter",
    metadata,
    Column("id", Text, primary_key=True),
    Column("key", Text, nullable=False),
    Column("scope_type", Text, nullable=False),
    Column("scope_id", Text, nullable=True),
    Column("value", Text, nullable=False),
    Column("value_type", Text, nullable=False),
    Column("owner_role", Text, nullable=False),
    Column("source_tag", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

audit_event_table = Table(
    "audit_event",
    metadata,
    Column("id", Text, primary_key=True),
    Column("actor_user_id", Text, nullable=True),
    Column("actor_role", Text, nullable=False),
    Column("action_code", Text, nullable=False),
    Column("subject_type", Text, nullable=False),
    Column("subject_id", Text, nullable=False),
    Column("rule_ref", Text, nullable=True),
    Column("metadata", JSONB, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)

state_transition_table = Table(
    "state_transition",
    metadata,
    Column("id", Text, primary_key=True),
    Column("machine", Text, nullable=False),
    Column("subject_type", Text, nullable=False),
    Column("subject_id", Text, nullable=False),
    Column("from_state", Text, nullable=True),
    Column("to_state", Text, nullable=False),
    Column("trigger", Text, nullable=False),
    Column("actor_user_id", Text, nullable=True),
    Column("actor_role", Text, nullable=False),
    Column("notes", JSONB, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)

idempotency_key_table = Table(
    "idempotency_key",
    metadata,
    Column("key", Text, primary_key=True),
    Column("principal_id", Text, primary_key=True),
    Column("request_hash", Text, nullable=False),
    Column("status_code", Integer, nullable=False),
    Column("response_body", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

terms_version_table = Table(
    "terms_version",
    metadata,
    Column("id", Text, primary_key=True),
    Column("code", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("mentions_pharmacy_service_fee", Boolean, nullable=False),
    Column("service_fee_amount", Numeric(14, 2, asdecimal=True), nullable=True),
    Column("mentions_multi_vendor", Boolean, nullable=False),
    Column("mentions_pms_aggregation", Boolean, nullable=False),
    Column("body_ref", Text, nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=False),
)
