"""0009 pharmacy signup — a pharmacy creates its own account from the phone;
the reviewer fills the licence number and dates when approving, so those
columns become nullable and a CHECK requires them on a VALID licence. Adds
`signup_attempt`, the table the sign-up rate limiter counts.

Forward-only (A2.7): `downgrade()` raises on purpose.
"""
from pathlib import Path

from alembic import op

revision = "0009_pharmacy_signup"
down_revision = "0008_credit_override"
branch_labels = None
depends_on = None

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0009_pharmacy_signup.sql"


def upgrade() -> None:
    op.execute(SQL_PATH.read_text())


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (A2.7)")
