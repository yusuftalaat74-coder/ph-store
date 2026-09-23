"""0001 initial schema — verbatim DDL from backend-spec.md A3.2 (66 tables).

Forward-only (A2.7): there is no downgrade path for a schema whose purpose is
append-only ledgers and audit trails. downgrade() raises on purpose.
"""
from pathlib import Path

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0001_initial.sql"


def upgrade() -> None:
    # Executed on the raw DBAPI cursor with zero arguments (not op.execute /
    # sa.text / exec_driver_sql), because this DDL legitimately contains both
    # ":name"-looking substrings (comments, "-- prefix:") and "%"
    # (PL/pgSQL RAISE format spec, a LIKE 'CFG-%' pattern) that SQLAlchemy's
    # and psycopg's own placeholder scanners would otherwise misparse — a
    # bare cursor.execute(sql) with no params argument does no such scanning.
    raw_connection = op.get_bind().connection
    cursor = raw_connection.cursor()
    cursor.execute(SQL_PATH.read_text())
    cursor.close()


def downgrade() -> None:
    raise NotImplementedError("ROVA migrations are forward-only (A2.7, B1.6).")
