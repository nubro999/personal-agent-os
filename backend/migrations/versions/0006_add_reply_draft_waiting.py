"""add reply draft waiting

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-03 15:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "reply_drafts", sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "reply_drafts",
        sa.Column("reminders", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("reply_drafts", "reminders")
    op.drop_column("reply_drafts", "waiting_since")
