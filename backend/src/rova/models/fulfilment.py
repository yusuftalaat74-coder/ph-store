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


delivery_job_table = Table(
    "delivery_job",
    metadata,
    Column("id", Text, primary_key=True),
    Column("order_id", Text, nullable=False),
    Column("vendor_id", Text, nullable=False),
    Column("pharmacy_id", Text, nullable=False),
    Column("performed_by", Text, nullable=False),
    Column("transporter_id", Text, nullable=True),
    Column("courier_user_id", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("fee_event_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

delivery_attempt_table = Table(
    "delivery_attempt",
    metadata,
    Column("id", Text, primary_key=True),
    Column("delivery_job_id", Text, nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("attempted_at", DateTime(timezone=True), nullable=False),
    Column("outcome", Text, nullable=False),
    Column("courier_user_id", Text, nullable=False),
    Column("proof_signature_or_code", Text, nullable=True),
    Column("proof_photo_ref", Text, nullable=True),
    Column("proof_captured_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

receipt_table = Table(
    "receipt",
    metadata,
    Column("id", Text, primary_key=True),
    Column("order_id", Text, nullable=False),
    Column("received_by_user_id", Text, nullable=True),
    Column("channel_ref", Text, nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

receipt_line_table = Table(
    "receipt_line",
    metadata,
    Column("id", Text, primary_key=True),
    Column("receipt_id", Text, nullable=False),
    Column("order_line_id", Text, nullable=False),
    Column("accepted_qty", Integer, nullable=False),
    Column("rejected_qty", Integer, nullable=False),
    Column("reason_code", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

dispute_table = Table(
    "dispute",
    metadata,
    Column("id", Text, primary_key=True),
    Column("order_id", Text, nullable=False),
    Column("receipt_line_id", Text, nullable=True),
    Column("invoice_line_id", Text, nullable=True),
    Column("type", Text, nullable=False),
    Column("raised_by", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("assigned_to_user_id", Text, nullable=True),
    Column("assigned_at", DateTime(timezone=True), nullable=True),
    Column("outcome", Text, nullable=True),
    Column("resolved_by_user_id", Text, nullable=True),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("notes", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

return_table = Table(
    "return",
    metadata,
    Column("id", Text, primary_key=True),
    Column("order_id", Text, nullable=False),
    Column("rma_number", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("requested_by_user_id", Text, nullable=True),
    Column("origin", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

return_line_table = Table(
    "return_line",
    metadata,
    Column("id", Text, primary_key=True),
    Column("return_id", Text, nullable=False),
    Column("order_line_id", Text, nullable=False),
    Column("qty", Integer, nullable=False),
    Column("reason_code", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

traceability_event_table = Table(
    "traceability_event",
    metadata,
    Column("id", Text, primary_key=True),
    Column("order_line_id", Text, nullable=False),
    Column("seal_id", Text, nullable=False),
    Column("batch_number", Text, nullable=False),
    Column("lot_number", Text, nullable=False),
    Column("expiry_date", Date, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("actor_user_id", Text, nullable=True),
    Column("actor_role", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)

eta_estimate_table = Table(
    "eta_estimate",
    metadata,
    Column("id", Text, primary_key=True),
    Column("order_id", Text, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
    Column("order_state", Text, nullable=False),
    Column("delivery_state", Text, nullable=True),
    Column("earliest_at", DateTime(timezone=True), nullable=False),
    Column("latest_at", DateTime(timezone=True), nullable=False),
    Column("basis", Text, nullable=False),
    Column("components", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("realised_at", DateTime(timezone=True), nullable=True),
    Column("error_minutes", Integer, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

sla_timer_table = Table(
    "sla_timer",
    metadata,
    Column("id", Text, primary_key=True),
    Column("policy_type", Text, nullable=False),
    Column("subject_type", Text, nullable=False),
    Column("subject_id", Text, nullable=False),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("config_source", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
