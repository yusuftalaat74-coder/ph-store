"""0004 product category — the axis the storefront filters on.

Nullable, and indexed only over published rows because that is the only
slice the catalogue endpoints ever read.

Forward-only (A2.7): `downgrade()` raises on purpose.
"""
from pathlib import Path

from alembic import op

revision = "0004_product_category"
down_revision = "0003_storefront_columns"
branch_labels = None
depends_on = None

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0004_product_category.sql"


def upgrade() -> None:
    op.execute(SQL_PATH.read_text())


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (A2.7)")
