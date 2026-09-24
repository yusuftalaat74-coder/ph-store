"""0005 PH Office link — integration_outbox / integration_inbound_event and the
nullable mirror columns of PH Office SPEC 5.9.1. Additive; inert while
ROVA_OFFICE_ENABLED=false.

Forward-only (A2.7): `downgrade()` raises on purpose.
"""
from pathlib import Path

from alembic import op

revision = "0005_office_link"
down_revision = "0004_product_category"
branch_labels = None
depends_on = None

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0005_office_link.sql"


def upgrade() -> None:
    op.execute(SQL_PATH.read_text())


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (A2.7)")
