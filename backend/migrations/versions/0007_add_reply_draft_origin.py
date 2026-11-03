"""add reply draft origin

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03 16:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "reply_drafts",
        sa.Column("origin", sa.Text(), server_default=sa.text("'mention'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_reply_drafts_origin"), "reply_drafts", "origin IN ('mention', 'case')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_reply_drafts_origin"), "reply_drafts", type_="check")
    op.drop_column("reply_drafts", "origin")
