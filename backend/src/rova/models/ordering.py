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


request_table = Table(
    "request",
    metadata,
    Column("id", Text, primary_key=True),
    Column("number", Text, nullable=False),
    Column("pharmacy_id", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("channel", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("allocation_strategy", Text, nullable=False),
    Column("confirmation_ref", Text, nullable=True),
    Column("confirmed_at", DateTime(timezone=True), nullable=True),
    Column("created_by_user_id", Text, nullable=True),
    Column("acting_ops_user_id", Text, nullable=True),
    Column("pms_idempotency_key", Text, nullable=True),
    Column("raw_payload_ref", Text, nullable=True),
    Column("raw_payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

request_line_table = Table(
    "request_line",
    metadata,
    Column("id", Text, primary_key=True),
    Column("request_id", Text, nullable=False),
    Column("index_product_id", Text, nullable=True),
    Column("qty_requested", Integer, nullable=False),
    Column("line_kind", Text, nullable=False),
    Column("match_status", Text, nullable=False),
    Column("confidence", Numeric(5, 4, asdecimal=True), nullable=True),
    Column("source_span", Text, nullable=True),
    Column("candidate_product_ids", JSONB, nullable=False),
    Column("substitution_id", Text, nullable=True),
    Column("origin_line_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

quotation_table = Table(
    "quotation",
    metadata,
    Column("id", Text, primary_key=True),
    Column("request_id", Text, nullable=False),
    Column("vendor_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("invited_at", DateTime(timezone=True), nullable=False),
    Column("submitted_at", DateTime(timezone=True), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

quotation_line_table = Table(
    "quotation_line",
    metadata,
    Column("id", Text, primary_key=True),
    Column("quotation_id", Text, nullable=False),
    Column("request_line_id", Text, nullable=False),
    Column("index_product_id", Text, nullable=False),
    Column("regulated_price", Boolean, nullable=False),
    Column("offered_qty", Integer, nullable=False),
    Column("price", Numeric(14, 2, asdecimal=True), nullable=True),
    Column("expiry_horizon_days", Integer, nullable=False),
    Column("accepted", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

order_table = Table(
    "order",
    metadata,
    Column("id", Text, primary_key=True),
    Column("number", Text, nullable=False),
    Column("request_id", Text, nullable=False),
    Column("vendor_id", Text, nullable=False),
    Column("pharmacy_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("cancel_reason", Text, nullable=True),
    Column("payment_terms", Text, nullable=False),
    Column("credit_days", Integer, nullable=True),
    Column("delivery_mode", Text, nullable=False),
    Column("accepted_at", DateTime(timezone=True), nullable=True),
    Column("promised_dispatch_at", DateTime(timezone=True), nullable=True),
    Column("dispatched_at", DateTime(timezone=True), nullable=True),
    Column("credit_override_user_id", Text, nullable=True),
    Column("credit_override_reason", Text, nullable=True),
    Column("compliance_flag", Boolean, nullable=False),
    Column("goods_total", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

order_line_table = Table(
    "order_line",
    metadata,
    Column("id", Text, primary_key=True),
    Column("order_id", Text, nullable=False),
    Column("request_line_id", Text, nullable=False),
    Column("index_product_id", Text, nullable=False),
    Column("regulated_price", Boolean, nullable=False),
    Column("ordered_qty", Integer, nullable=False),
    Column("confirmed_qty", Integer, nullable=False),
    Column("unit_price", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("price_source", Text, nullable=False),
    Column("price_source_id", Text, nullable=False),
    Column("fulfilment_status", Text, nullable=False),
    Column("short_reason", Text, nullable=True),
    Column("batch_number", Text, nullable=True),
    Column("lot_number", Text, nullable=True),
    Column("expiry_date", Date, nullable=True),
    Column("seal_ids", ARRAY(Text), nullable=False),
    Column("near_expiry_ack_user_id", Text, nullable=True),
    Column("picked_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

allocation_table = Table(
    "allocation",
    metadata,
    Column("id", Text, primary_key=True),
    Column("request_line_id", Text, nullable=False),
    Column("vendor_id", Text, nullable=False),
    Column("order_line_id", Text, nullable=True),
    Column("reason_code", Text, nullable=False),
    Column("override_reason", Text, nullable=True),
    Column("ops_override_code", Text, nullable=True),
    Column("strategy", Text, nullable=False),
    Column("sequence_snapshot", JSONB, nullable=False),
    Column("inputs_fingerprint", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

demand_gap_table = Table(
    "demand_gap",
    metadata,
    Column("id", Text, primary_key=True),
    Column("index_product_id", Text, nullable=True),
    Column("raw_product_text", Text, nullable=True),
    Column("pharmacy_id", Text, nullable=False),
    Column("region_code", Text, nullable=False),
    Column("qty_requested", Integer, nullable=False),
    Column("cause", Text, nullable=False),
    Column("request_line_id", Text, nullable=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("resolved_by_offer_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
