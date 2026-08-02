"""add cross-host migration facts

Revision ID: a3f8c21d4e90
Revises: 51d85fbf3b61
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f8c21d4e90"
down_revision: str | None = "51d85fbf3b61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task", sa.Column("provisioned_by", sa.String(), nullable=True))
    op.add_column("task", sa.Column("workspace_verified_by", sa.String(), nullable=True))
    op.add_column("task", sa.Column("migration", sa.JSON(), nullable=True))
    # Existing branch records were created by the current claimant. Preserve their working
    # single-host semantics across the rollout; an unclaimed legacy checkout remains deliberately
    # unverified until a host claims and adopts it.
    op.execute(
        "UPDATE task SET provisioned_by = claimed_by, workspace_verified_by = claimed_by "
        "WHERE branch IS NOT NULL AND clone IS NOT NULL AND claimed_by IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("task", "migration")
    op.drop_column("task", "workspace_verified_by")
    op.drop_column("task", "provisioned_by")
