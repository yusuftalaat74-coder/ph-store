"""0006 one cart per pharmacy — an explicit `is_cart` flag and a partial
unique index over it, so the cart stops being inferred from mode + channel.

Forward-only (A2.7): `downgrade()` raises on purpose.
"""
from pathlib import Path

from alembic import op

revision = "0006_one_cart_per_pharmacy"
down_revision = "0005_cart_price_seen"
branch_labels = None
depends_on = None

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0006_one_cart_per_pharmacy.sql"


def upgrade() -> None:
    op.execute(SQL_PATH.read_text())


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (A2.7)")
