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


index_product_table = Table(
    "index_product",
    metadata,
    Column("id", Text, primary_key=True),
    Column("inn", Text, nullable=False),
    Column("brand_name", Text, nullable=True),
    Column("form", Text, nullable=False),
    Column("strength", Text, nullable=False),
    Column("pack_size", Text, nullable=False),
    Column("manufacturer", Text, nullable=False),
    Column("barcode", Text, nullable=True),
    Column("therapeutic_class", Text, nullable=True),
    Column("aim_status", Text, nullable=False),
    Column("regulated_price", Boolean, nullable=False),
    Column("review_status", Text, nullable=False),
    Column("reviewer_ref", Text, nullable=False),
    Column("search_text", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

product_alias_table = Table(
    "product_alias",
    metadata,
    Column("id", Text, primary_key=True),
    Column("index_product_id", Text, nullable=False),
    Column("alias_text", Text, nullable=False),
    Column("alias_normalised", Text, nullable=False),
    Column("alias_type", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("approved_by_ref", Text, nullable=True),
    Column("proposal_count", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

substitution_table = Table(
    "substitution",
    metadata,
    Column("id", Text, primary_key=True),
    Column("source_product_id", Text, nullable=False),
    Column("substitute_product_id", Text, nullable=False),
    Column("rationale", Text, nullable=False),
    Column("reviewer_ref", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

price_reference_table = Table(
    "price_reference",
    metadata,
    Column("id", Text, primary_key=True),
    Column("index_product_id", Text, nullable=False),
    Column("pvp_price", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("wholesale_derived_price", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("cif_basis", Numeric(14, 2, asdecimal=True), nullable=True),
    Column("effective_from", Date, nullable=False),
    Column("source_document", Text, nullable=False),
    Column("superseded_by", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

external_ref_table = Table(
    "external_ref",
    metadata,
    Column("id", Text, primary_key=True),
    Column("entity_type", Text, nullable=False),
    Column("local_id", Text, nullable=False),
    Column("system", Text, nullable=False),
    Column("external_id", Text, nullable=False),
    Column("mapping_source", Text, nullable=False),
    Column("mapped_at", DateTime(timezone=True), nullable=False),
)

vendor_offer_table = Table(
    "vendor_offer",
    metadata,
    Column("id", Text, primary_key=True),
    Column("vendor_id", Text, nullable=False),
    Column("index_product_id", Text, nullable=False),
    Column("regulated_price", Boolean, nullable=False),
    Column("qty_available", Integer, nullable=False),
    Column("pack_size", Text, nullable=False),
    Column("min_order_qty", Integer, nullable=True),
    Column("expiry_horizon_days", Integer, nullable=False),
    Column("price", Numeric(14, 2, asdecimal=True), nullable=True),
    Column("stock_confirmed_at", DateTime(timezone=True), nullable=False),
    Column("freshness_state", Text, nullable=False),
    Column("withdrawn_at", DateTime(timezone=True), nullable=True),
    Column("source_row_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

vendor_column_mapping_table = Table(
    "vendor_column_mapping",
    metadata,
    Column("id", Text, primary_key=True),
    Column("vendor_id", Text, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("mapping", JSONB, nullable=False),
    Column("sheet_selector", Text, nullable=True),
    Column("header_row_index", Integer, nullable=False),
    Column("unit_conventions", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_by_user_id", Text, nullable=False),
    Column("last_used_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

price_list_version_table = Table(
    "price_list_version",
    metadata,
    Column("id", Text, primary_key=True),
    Column("vendor_id", Text, nullable=False),
    Column("version_number", Integer, nullable=False),
    Column("source_file_ref", Text, nullable=False),
    Column("source_file_name", Text, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("mapping_id", Text, nullable=True),
    Column("uploaded_by_user_id", Text, nullable=False),
    Column("uploaded_at", DateTime(timezone=True), nullable=False),
    Column("partial_update", Boolean, nullable=False),
    Column("qty_only", Boolean, nullable=False),
    Column("effective_from", DateTime(timezone=True), nullable=True),
    Column("status", Text, nullable=False),
    Column("row_count", Integer, nullable=False),
    Column("accepted_rows", Integer, nullable=False),
    Column("rejected_rows", Integer, nullable=False),
    Column("warning_rows", Integer, nullable=False),
    Column("pending_rows", Integer, nullable=False),
    Column("supersedes_id", Text, nullable=True),
    Column("report_ref", Text, nullable=True),
    Column("went_live_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

price_list_row_table = Table(
    "price_list_row",
    metadata,
    Column("id", Text, primary_key=True),
    Column("version_id", Text, nullable=False),
    Column("row_index", Integer, nullable=False),
    Column("raw_values", JSONB, nullable=False),
    Column("vendor_sku", Text, nullable=True),
    Column("index_product_id", Text, nullable=True),
    Column("match_confidence", Numeric(5, 4, asdecimal=True), nullable=True),
    Column("candidate_product_ids", JSONB, nullable=False),
    Column("price_to_pharmacy", Numeric(14, 2, asdecimal=True), nullable=True),
    Column("price_to_public", Numeric(14, 2, asdecimal=True), nullable=True),
    Column("discount_pct", Numeric(7, 4, asdecimal=True), nullable=True),
    Column("vendor_cost", Numeric(14, 2, asdecimal=True), nullable=True),
    Column("qty_available", Integer, nullable=True),
    Column("pack_size", Text, nullable=True),
    Column("expiry_horizon_days", Integer, nullable=True),
    Column("min_order_qty", Integer, nullable=True),
    Column("outcome", Text, nullable=False),
    Column("outcome_reason", Text, nullable=True),
    Column("outcome_detail", Text, nullable=True),
    Column("resolved_by_user_id", Text, nullable=True),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("resulting_offer_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
