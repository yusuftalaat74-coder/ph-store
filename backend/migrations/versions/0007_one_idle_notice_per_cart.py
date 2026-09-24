"""0007 one idle-basket notice per basket — a partial unique index on the
payload's request_id, so the once-only rule is the database's and not a
check-then-insert that two overlapping ticks can both pass.

Forward-only (A2.7): `downgrade()` raises on purpose.
"""
from pathlib import Path

from alembic import op

revision = "0007_one_idle_notice_per_cart"
down_revision = "0006_one_cart_per_pharmacy"
branch_labels = None
depends_on = None

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0007_one_idle_notice_per_cart.sql"


def upgrade() -> None:
    op.execute(SQL_PATH.read_text())


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (A2.7)")
