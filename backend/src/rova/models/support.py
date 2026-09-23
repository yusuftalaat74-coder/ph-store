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


support_thread_table = Table(
    "support_thread",
    metadata,
    Column("id", Text, primary_key=True),
    Column("pharmacy_id", Text, nullable=False),
    Column("active_handler", Text, nullable=False),
    Column("active_ticket_id", Text, nullable=True),
    Column("last_activity_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

ticket_table = Table(
    "ticket",
    metadata,
    Column("id", Text, primary_key=True),
    Column("thread_id", Text, nullable=False),
    Column("subject_org_id", Text, nullable=False),
    Column("raised_by_user_id", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("related_order_id", Text, nullable=True),
    Column("requires_vendor_relay", Boolean, nullable=False),
    Column("opened_by_agent_user_id", Text, nullable=True),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

thread_message_table = Table(
    "thread_message",
    metadata,
    Column("id", Text, primary_key=True),
    Column("thread_id", Text, nullable=False),
    Column("sender_type", Text, nullable=False),
    Column("sender_user_id", Text, nullable=True),
    Column("channel", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("ticket_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

notification_table = Table(
    "notification",
    metadata,
    Column("id", Text, primary_key=True),
    Column("event_code", Text, nullable=False),
    Column("recipient_role", Text, nullable=True),
    Column("recipient_user_id", Text, nullable=True),
    Column("recipient_org_id", Text, nullable=True),
    Column("channel", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("rendered_text", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("sent_at", DateTime(timezone=True), nullable=True),
    Column("read_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
