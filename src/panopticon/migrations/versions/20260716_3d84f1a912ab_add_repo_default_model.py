"""add repo default model

Revision ID: 3d84f1a912ab
Revises: efba3610296a
Create Date: 2026-07-16 07:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3d84f1a912ab"
down_revision: str | None = "efba3610296a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("repo", schema=None) as batch_op:
        batch_op.add_column(sa.Column("default_model", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("repo", schema=None) as batch_op:
        batch_op.drop_column("default_model")
