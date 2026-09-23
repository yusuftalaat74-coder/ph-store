"""The append-only tables must refuse TRUNCATE, not only UPDATE and DELETE.

Found by an independent re-verification on 23 Sep 2026: the 0001 triggers are
`FOR EACH ROW`, and Postgres produces no per-row events for TRUNCATE, so
`TRUNCATE state_transition` succeeded and emptied the table. Migration 0002
adds the statement-level guard. These tests are what stop it coming back.
"""
import pytest
from sqlalchemy import text

APPEND_ONLY = ("ledger_entry", "audit_event", "state_transition", "traceability_event")


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_truncate_is_refused(db_engine, table):
    with pytest.raises(Exception) as excinfo:
        with db_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {table}"))
    assert "append-only" in str(excinfo.value).lower()


def test_delete_of_an_existing_row_is_still_refused(db_engine):
    """A row-level trigger only fires when there is a row, so this test has to
    create one first — deleting from an empty table raises nothing and would
    have passed for the wrong reason."""
    with db_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO audit_event (id, actor_role, action_code, subject_type, subject_id) "
                 "VALUES ('aud_trunc_guard_probe', 'PlatformAdmin', 'CONFIG_PARAMETER_CHANGE', "
                 "'config_parameter', 'probe')"))

    with pytest.raises(Exception) as excinfo:
        with db_engine.begin() as conn:
            conn.execute(text("DELETE FROM audit_event WHERE id='aud_trunc_guard_probe'"))
    assert "append-only" in str(excinfo.value).lower()

    with pytest.raises(Exception) as excinfo:
        with db_engine.begin() as conn:
            conn.execute(text("UPDATE audit_event SET actor_role='X' WHERE id='aud_trunc_guard_probe'"))
    assert "append-only" in str(excinfo.value).lower()


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_both_guards_are_installed(db_engine, table):
    """Belt and braces: assert the two trigger kinds exist on each table, so a
    future migration that drops one is caught here rather than in production."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tgname, tgtype FROM pg_trigger "
                 "WHERE tgrelid = CAST(:t AS regclass) AND NOT tgisinternal"),
            {"t": table},
        ).mappings().all()
    names = {r["tgname"] for r in rows}
    assert any(n.endswith("_append_only") for n in names), f"{table} lost its row-level guard"
    assert any(n.endswith("_no_truncate") for n in names), f"{table} lost its TRUNCATE guard"
