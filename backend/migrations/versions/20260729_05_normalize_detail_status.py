"""Normalize the retry status written by the first retry-state migration."""

from alembic import op
import sqlalchemy as sa

revision = "20260729_05"
down_revision = "20260729_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE user_purchases "
            "SET detail_status = :normalized "
            "WHERE detail_status = :quoted"
        ).bindparams(normalized="pending", quoted="'pending'")
    )


def downgrade() -> None:
    # Normalized status values are valid for both schema revisions.
    pass
