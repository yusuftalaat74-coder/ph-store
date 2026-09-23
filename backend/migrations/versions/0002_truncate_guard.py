"""0002 truncate guard — close the TRUNCATE hole in the append-only tables.

0001's `BEFORE UPDATE OR DELETE ... FOR EACH ROW` triggers cannot see a
TRUNCATE, so the four append-only tables could be emptied in one statement.
This adds the matching `BEFORE TRUNCATE ... FOR EACH STATEMENT` triggers.

Forward-only (A2.7): `downgrade()` raises on purpose.
"""
from pathlib import Path

from alembic import op

revision = "0002_truncate_guard"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0002_truncate_guard.sql"


def upgrade() -> None:
    # Raw cursor for the same reason as 0001: the DDL carries "%" inside the
    # reused PL/pgSQL RAISE format spec, which SQLAlchemy's and psycopg's
    # placeholder scanners would misparse.
    raw_connection = op.get_bind().connection
    cursor = raw_connection.cursor()
    cursor.execute(SQL_PATH.read_text())
    cursor.close()


def downgrade() -> None:
    raise NotImplementedError(
        "migrations are forward-only (A2.7); dropping the TRUNCATE guard would "
        "re-open the hole this migration exists to close"
    )
