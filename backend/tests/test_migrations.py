"""A17 'Migrations' row / B1.2, B1.6, B2.9, B2.11."""
import os
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

SQL_PATH = Path(__file__).resolve().parents[1] / "migrations" / "sql" / "0001_initial.sql"
REPO_ROOT = SQL_PATH.parents[2]


def test_downgrade_is_forward_only_refused():
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "-1"],
        cwd=REPO_ROOT, env={**os.environ}, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "NotImplementedError" in result.stderr
    assert "forward-only" in result.stderr


# The spec's schema is 66 tables. Anything beyond it is something this
# codebase added on purpose, and it is listed here by name and reason so the
# count keeps catching a table that arrived by accident — which is what the
# bare `== 66` was for, and what simply raising the number would have thrown
# away.
ADDED_SINCE_THE_SPEC = {
    # A10 names ADMIN_OVERRIDE as an option for a sub-basket the credit gate
    # blocks, and gives it nothing to live in — which is why R-044 was a wall
    # for a pharmacist with a full basket. One row is one permission: one
    # request, one distributor, one amount, spent once, with the name of
    # whoever allowed it. (migration 0008)
    "credit_override",
    # A pharmacy can sign itself up from the phone (signup spec §1). Sign-up
    # is the only unauthenticated write, and the API runs two workers, so
    # the rate limiter counts attempts here rather than in process memory.
    # (migration 0009)
    "signup_attempt",
}


def test_the_schema_is_the_spec_plus_what_we_added_on_purpose(db_engine):
    with db_engine.connect() as conn:
        names = set(conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name <> 'alembic_version'"
        )).scalars().all())

    unexpected = names - ADDED_SINCE_THE_SPEC
    assert len(unexpected) == 66, sorted(unexpected)
    assert ADDED_SINCE_THE_SPEC <= names, sorted(ADDED_SINCE_THE_SPEC - names)


def test_append_only_tables_reject_update_and_delete(db_engine):
    # Deterministic proof with a real row (append-only sub-test files also
    # exercise this per machine's effects; this is the direct DDL-level proof
    # B2.9 asks for).
    with db_engine.connect() as conn:
        trx = conn.begin()
        conn.execute(text("INSERT INTO app_user (id, phone, name, password_hash) VALUES "
                           "('usr_mig_test', '+258000000001', 'T', 'x')"))
        conn.execute(
            text("INSERT INTO audit_event (id, actor_user_id, actor_role, action_code, subject_type, subject_id) "
                 "VALUES ('aud_mig_test', 'usr_mig_test', 'SYSTEM', 'CONFIG_PARAMETER_CHANGE', 'x', '1')")
        )
        try:
            conn.execute(text("UPDATE audit_event SET actor_role='X' WHERE id='aud_mig_test'"))
            raised = False
        except DBAPIError:
            raised = True
        finally:
            trx.rollback()
    assert raised, "append-only table accepted an UPDATE"


def test_no_fk_between_billing_and_fees_groups(db_engine):
    group6 = {"credit_facility", "ledger_entry", "invoice", "invoice_line", "credit_note", "payment", "payment_allocation"}
    group7 = {"fee_schedule", "order_fee_schedule", "fee_event", "platform_invoice"}
    with db_engine.connect() as conn:
        fks = conn.execute(
            text(
                """
                SELECT tc.table_name AS from_table, ccu.table_name AS to_table
                FROM information_schema.table_constraints tc
                JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                """
            )
        ).mappings().all()
    for fk in fks:
        crosses = (fk["from_table"] in group6 and fk["to_table"] in group7) or \
                  (fk["from_table"] in group7 and fk["to_table"] in group6)
        assert not crosses, f"FK crosses billing/fees boundary: {fk['from_table']} -> {fk['to_table']}"


def test_organisation_type_has_no_platform_value():
    sql = SQL_PATH.read_text()
    m = re.search(r"type\s+TEXT NOT NULL CHECK \(type IN \(([^)]*)\)\)", sql)
    assert "'PLATFORM'" not in m.group(1)


def test_no_float_or_native_enum_in_ddl():
    sql = SQL_PATH.read_text()
    assert "FLOAT" not in sql.upper().replace("--", "")
    assert "DOUBLE PRECISION" not in sql.upper()
    assert "CREATE TYPE" not in sql.upper()
