"""add stage entry wake status

Revision ID: 51d85fbf3b61
Revises: d51b047f2a8c
Create Date: 2026-07-31 05:10:15.107274
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "51d85fbf3b61"
down_revision: str | None = "d51b047f2a8c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("history", schema=None) as batch_op:
        batch_op.add_column(sa.Column("wake_status", sa.String(), nullable=True))
    op.execute(sa.text("UPDATE history SET wake_status = 'skipped'"))
    with op.batch_alter_table("history", schema=None) as batch_op:
        batch_op.alter_column(
            "wake_status",
            existing_type=sa.String(),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("history", schema=None) as batch_op:
        batch_op.drop_column("wake_status")
