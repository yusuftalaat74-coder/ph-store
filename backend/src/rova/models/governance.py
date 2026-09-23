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


mode_switch_table = Table(
    "mode_switch",
    metadata,
    Column("id", Text, primary_key=True),
    Column("switch_key", Text, nullable=False),
    Column("scope_type", Text, nullable=False),
    Column("scope_id", Text, nullable=True),
    Column("current_value", Text, nullable=False),
    Column("previous_value", Text, nullable=True),
    Column("proposed_value", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("gate_results", JSONB, nullable=False),
    Column("gates_checked_at", DateTime(timezone=True), nullable=True),
    Column("proposed_by_user_id", Text, nullable=True),
    Column("flipped_by_user_id", Text, nullable=True),
    Column("flipped_at", DateTime(timezone=True), nullable=True),
    Column("observation_ends_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

ranking_isolation_audit_table = Table(
    "ranking_isolation_audit",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_at", DateTime(timezone=True), nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("sample_size", Integer, nullable=False),
    Column("allocations_sampled", JSONB, nullable=False),
    Column("divergences", Integer, nullable=False),
    Column("manual_ops_share", Numeric(7, 4, asdecimal=True), nullable=False),
    Column("manual_ops_top_vendor_share", Numeric(7, 4, asdecimal=True), nullable=False),
    Column("auto_top_vendor_share", Numeric(7, 4, asdecimal=True), nullable=False),
    Column("result", Text, nullable=False),
    Column("evidence_ref", Text, nullable=False),
    Column("triggered_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

vendor_score_table = Table(
    "vendor_score",
    metadata,
    Column("id", Text, primary_key=True),
    Column("vendor_id", Text, nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("fill_rate", Numeric(7, 4, asdecimal=True), nullable=False),
    Column("on_time_dispatch_rate", Numeric(7, 4, asdecimal=True), nullable=False),
    Column("promise_accuracy", Numeric(7, 4, asdecimal=True), nullable=False),
    Column("eta_accuracy", Numeric(7, 4, asdecimal=True), nullable=False),
    Column("quotation_sla_breaches", Integer, nullable=False),
    Column("sample_orders", Integer, nullable=False),
    Column("score_value", Numeric(7, 2, asdecimal=True), nullable=False),
    Column("formula_ref", Text, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
