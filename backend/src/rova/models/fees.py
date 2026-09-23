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


fee_schedule_table = Table(
    "fee_schedule",
    metadata,
    Column("id", Text, primary_key=True),
    Column("type", Text, nullable=False),
    Column("payer", Text, nullable=False),
    Column("rate_or_amount", Numeric(14, 4, asdecimal=True), nullable=False),
    Column("base", Text, nullable=True),
    Column("applies_to", Text, nullable=False),
    Column("scope_type", Text, nullable=False),
    Column("scope_id", Text, nullable=True),
    Column("effective_from", Date, nullable=False),
    Column("effective_to", Date, nullable=True),
    Column("earning_event", Text, nullable=False),
    Column("legal_status", Text, nullable=False),
    Column("agreement_id", Text, nullable=True),
    Column("notes", Text, nullable=True),
    Column("created_by_user_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

order_fee_schedule_table = Table(
    "order_fee_schedule",
    metadata,
    Column("order_id", Text, primary_key=True),
    Column("fee_schedule_id", Text, primary_key=True),
)

fee_event_table = Table(
    "fee_event",
    metadata,
    Column("id", Text, primary_key=True),
    Column("fee_schedule_id", Text, nullable=False),
    Column("payer_org_id", Text, nullable=False),
    Column("request_id", Text, nullable=True),
    Column("order_id", Text, nullable=True),
    Column("delivery_job_id", Text, nullable=True),
    Column("base_amount", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("amount", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("attribution", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("reversed_at", DateTime(timezone=True), nullable=True),
    Column("reversal_reason", Text, nullable=True),
    Column("platform_invoice_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

platform_invoice_table = Table(
    "platform_invoice",
    metadata,
    Column("id", Text, primary_key=True),
    Column("number", Text, nullable=False),
    Column("payer_org_id", Text, nullable=False),
    Column("period_start", Date, nullable=False),
    Column("period_end", Date, nullable=False),
    Column("amount", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
