"""0005 cart price seen — the price and vendor a line was added at.

Forward-only (A2.7): `downgrade()` raises on purpose.
"""
from pathlib import Path

from alembic import op

revision = "0005_cart_price_seen"
down_revision = "0004_product_category"
branch_labels = None
depends_on = None

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0005_cart_price_seen.sql"


def upgrade() -> None:
    op.execute(SQL_PATH.read_text())


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (A2.7)")
