"""add task attention marker

Revision ID: 2418d77fcee3
Revises: 3d84f1a912ab
Create Date: 2026-07-31 04:39:42.010957
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2418d77fcee3"
down_revision: str | None = "3d84f1a912ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("attention", sa.Boolean(), nullable=True))
    op.execute(sa.text("UPDATE task SET attention = false WHERE attention IS NULL"))
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.alter_column("attention", existing_type=sa.Boolean(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("attention")
