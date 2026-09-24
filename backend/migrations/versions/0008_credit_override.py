"""0008 credit override — a pharmacy can ask for its limit to be exceeded,
and the distributor's or the platform's people can allow it, once, for one
order, with their name on it.

Forward-only (A2.7): `downgrade()` raises on purpose.
"""
from pathlib import Path

from alembic import op

revision = "0008_credit_override"
down_revision = "0007_one_idle_notice_per_cart"
branch_labels = None
depends_on = None

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0008_credit_override.sql"


def upgrade() -> None:
    op.execute(SQL_PATH.read_text())


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (A2.7)")
