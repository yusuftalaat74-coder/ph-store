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


organisation_table = Table(
    "organisation",
    metadata,
    Column("id", Text, primary_key=True),
    Column("tax_id", Text, nullable=False),
    Column("legal_name", Text, nullable=False),
    Column("legal_name_normalised", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("country", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

app_user_table = Table(
    "app_user",
    metadata,
    Column("id", Text, primary_key=True),
    Column("phone", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("locale", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("failed_login_count", Integer, nullable=False),
    Column("locked_until", DateTime(timezone=True), nullable=True),
    Column("erased_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

membership_table = Table(
    "membership",
    metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("organisation_id", Text, nullable=True),
    Column("role_codes", ARRAY(Text), nullable=False),
    Column("status", Text, nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

auth_session_table = Table(
    "auth_session",
    metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("membership_id", Text, nullable=True),
    Column("surface", Text, nullable=False),
    Column("refresh_token_hash", Text, nullable=False),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)

pharmacy_account_table = Table(
    "pharmacy_account",
    metadata,
    Column("id", Text, primary_key=True),
    Column("organisation_id", Text, nullable=False),
    Column("region_code", Text, nullable=False),
    Column("licence_type", Text, nullable=False),
    Column("trade_name", Text, nullable=False),
    Column("address", Text, nullable=False),
    Column("latitude", Numeric(9, 6, asdecimal=True), nullable=False),
    Column("longitude", Numeric(9, 6, asdecimal=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("suspension_cause", Text, nullable=True),
    Column("auto_reroute", Boolean, nullable=False),
    Column("buyer_approval_threshold", Numeric(14, 2, asdecimal=True), nullable=True),
    Column("default_allocation_strategy", Text, nullable=True),
    Column("is_assisted", Boolean, nullable=False),
    Column("assisted_channel_ref", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

vendor_account_table = Table(
    "vendor_account",
    metadata,
    Column("id", Text, primary_key=True),
    Column("organisation_id", Text, nullable=False),
    Column("region_code", Text, nullable=False),
    Column("trade_name", Text, nullable=False),
    Column("vendor_type", Text, nullable=False),
    Column("delivery_mode", Text, nullable=False),
    Column("mov_amount", Numeric(14, 2, asdecimal=True), nullable=False),
    Column("acceptance_mode", Text, nullable=False),
    Column("sourcing_attestation", Boolean, nullable=False),
    Column("locale", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("suspension_cause", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

transporter_account_table = Table(
    "transporter_account",
    metadata,
    Column("id", Text, primary_key=True),
    Column("organisation_id", Text, nullable=True),
    Column("name", Text, nullable=False),
    Column("pool_type", Text, nullable=False),
    Column("licence_id", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

licence_table = Table(
    "licence",
    metadata,
    Column("id", Text, primary_key=True),
    Column("holder_type", Text, nullable=False),
    Column("holder_id", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("number", Text, nullable=False),
    Column("issuer", Text, nullable=False),
    Column("issue_date", Date, nullable=False),
    Column("expiry_date", Date, nullable=False),
    Column("document_ref", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("successor_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

verification_case_table = Table(
    "verification_case",
    metadata,
    Column("id", Text, primary_key=True),
    Column("subject_type", Text, nullable=False),
    Column("organisation_id", Text, nullable=False),
    Column("licence_id", Text, nullable=True),
    Column("reviewer_user_id", Text, nullable=True),
    Column("decision", Text, nullable=False),
    Column("decision_notes", Text, nullable=True),
    Column("opened_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

vendor_agreement_table = Table(
    "vendor_agreement",
    metadata,
    Column("id", Text, primary_key=True),
    Column("vendor_id", Text, nullable=False),
    Column("signed_at", DateTime(timezone=True), nullable=False),
    Column("document_ref", Text, nullable=False),
    Column("founding_supplier", Boolean, nullable=False),
    Column("multi_vendor_clause_ack", Boolean, nullable=False),
    Column("exclusivity_until", Date, nullable=True),
    Column("preferential_rate_until", Date, nullable=True),
    Column("badge_until", Date, nullable=True),
    Column("waived_by_user_id", Text, nullable=True),
    Column("waived_at", DateTime(timezone=True), nullable=True),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

vendor_pharmacy_relationship_table = Table(
    "vendor_pharmacy_relationship",
    metadata,
    Column("id", Text, primary_key=True),
    Column("vendor_id", Text, nullable=False),
    Column("pharmacy_id", Text, nullable=False),
    Column("declared_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("evidence_ref", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

consent_record_table = Table(
    "consent_record",
    metadata,
    Column("id", Text, primary_key=True),
    Column("subject_type", Text, nullable=False),
    Column("subject_id", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("terms_version_id", Text, nullable=True),
    Column("granted_by_user_id", Text, nullable=True),
    Column("channel_ref", Text, nullable=True),
    Column("granted_at", DateTime(timezone=True), nullable=False),
    Column("withdrawn_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

data_subject_request_table = Table(
    "data_subject_request",
    metadata,
    Column("id", Text, primary_key=True),
    Column("subject_user_id", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("handled_by_user_id", Text, nullable=True),
    Column("result_ref", Text, nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("fulfilled_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
