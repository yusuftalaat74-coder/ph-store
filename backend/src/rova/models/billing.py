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


credit_facility_table = Table(
    "credit_facility",
    metadata,
    Column("id", Text, primary_key=True),
    Column("vendor_id", Text, nullable=False),
    Column("pharmacy_id", Text, nullable=False),
    Column("limit_amount", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("opening_balance", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("opening_balance_attested_at", DateTime(timezone=True), nullable=True),
    Column("terms_days", Integer, nullable=True),
    Column("mov_waived", Boolean, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

ledger_entry_table = Table(
    "ledger_entry",
    metadata,
    Column("id", Text, primary_key=True),
    Column("credit_facility_id", Text, nullable=False),
    Column("entry_type", Text, nullable=False),
    Column("reference_type", Text, nullable=False),
    Column("reference_id", Text, nullable=False),
    Column("amount", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

invoice_table = Table(
    "invoice",
    metadata,
    Column("id", Text, primary_key=True),
    Column("order_id", Text, nullable=False),
    Column("issuer_vendor_id", Text, nullable=False),
    Column("pharmacy_id", Text, nullable=False),
    Column("vendor_invoice_number", Text, nullable=False),
    Column("total_amount", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("price_match_flag", Text, nullable=False),
    Column("document_ref", Text, nullable=True),
    Column("uploaded_at", DateTime(timezone=True), nullable=False),
    Column("finalised_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

invoice_line_table = Table(
    "invoice_line",
    metadata,
    Column("id", Text, primary_key=True),
    Column("invoice_id", Text, nullable=False),
    Column("order_line_id", Text, nullable=False),
    Column("invoiced_price", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("invoiced_qty", Integer, nullable=False),
    Column("match_result", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

credit_note_table = Table(
    "credit_note",
    metadata,
    Column("id", Text, primary_key=True),
    Column("vendor_id", Text, nullable=False),
    Column("pharmacy_id", Text, nullable=False),
    Column("order_id", Text, nullable=False),
    Column("return_id", Text, nullable=True),
    Column("dispute_id", Text, nullable=True),
    Column("amount", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("approved_by_compliance_user_id", Text, nullable=True),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

payment_table = Table(
    "payment",
    metadata,
    Column("id", Text, primary_key=True),
    Column("pharmacy_id", Text, nullable=False),
    Column("vendor_id", Text, nullable=False),
    Column("method", Text, nullable=False),
    Column("collected_by", Text, nullable=False),
    Column("amount", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("external_ref", Text, nullable=True),
    Column("recorded_by_user_id", Text, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

payment_allocation_table = Table(
    "payment_allocation",
    metadata,
    Column("id", Text, primary_key=True),
    Column("payment_id", Text, nullable=False),
    Column("invoice_id", Text, nullable=False),
    Column("amount", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
