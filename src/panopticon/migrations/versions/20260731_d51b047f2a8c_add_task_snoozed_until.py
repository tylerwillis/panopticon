"""add task snoozed until

Revision ID: d51b047f2a8c
Revises: 2418d77fcee3
Create Date: 2026-07-31 05:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d51b047f2a8c"
down_revision: str | None = "2418d77fcee3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("snoozed_until", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("snoozed_until")
