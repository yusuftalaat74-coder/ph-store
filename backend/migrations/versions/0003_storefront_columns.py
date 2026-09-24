"""0003 storefront columns — a vendor's published list price, and a place
for a real product image.

Both nullable: every existing row stays valid, and the API treats a missing
list price as "no margin to show" rather than inventing one.

Forward-only (A2.7): `downgrade()` raises on purpose.
"""
from pathlib import Path

from alembic import op

revision = "0003_storefront_columns"
down_revision = "0002_truncate_guard"
branch_labels = None
depends_on = None

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0003_storefront_columns.sql"


def upgrade() -> None:
    op.execute(SQL_PATH.read_text())


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (A2.7)")
