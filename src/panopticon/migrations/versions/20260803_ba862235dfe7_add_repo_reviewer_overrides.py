"""add repo reviewer overrides

Revision ID: ba862235dfe7
Revises: baa229ad49e8
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ba862235dfe7"
down_revision: str | None = "baa229ad49e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("repo", sa.Column("honesty_reviewer", sa.String(), nullable=True))
    op.add_column("repo", sa.Column("reviewer_1", sa.String(), nullable=True))
    op.add_column("repo", sa.Column("reviewer_2", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("repo", "reviewer_2")
    op.drop_column("repo", "reviewer_1")
    op.drop_column("repo", "honesty_reviewer")
