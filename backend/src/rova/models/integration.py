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


pms_link_table = Table(
    "pms_link",
    metadata,
    Column("id", Text, primary_key=True),
    Column("pharmacy_id", Text, nullable=False),
    Column("pms_tenant_id", Text, nullable=False),
    Column("api_key_hash", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("consent_record_id", Text, nullable=True),
    Column("product_key_mode", Text, nullable=False),
    Column("linked_at", DateTime(timezone=True), nullable=True),
    Column("last_inbound_at", DateTime(timezone=True), nullable=True),
    Column("last_outbound_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

dispensing_aggregate_table = Table(
    "dispensing_aggregate",
    metadata,
    Column("id", Text, primary_key=True),
    Column("pms_link_id", Text, nullable=False),
    Column("index_product_id", Text, nullable=False),
    Column("period_date", Date, nullable=False),
    Column("dispensed_units", Integer, nullable=False),
    Column("stock_on_hand", Integer, nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False),
)
