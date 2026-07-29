"""Persist bounded platform product-detail retry state."""

from alembic import op
import sqlalchemy as sa

revision = "20260729_04"
down_revision = "20260729_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("user_purchases")
    }
    additions = (
        ("detail_attempts", sa.Integer(), "0"),
        ("detail_status", sa.String(), sa.text("'pending'")),
        ("detail_last_attempt_at", sa.DateTime(), None),
        ("detail_next_retry_at", sa.DateTime(), None),
    )
    for name, column_type, server_default in additions:
        if name in columns:
            continue
        op.add_column(
            "user_purchases",
            sa.Column(
                name,
                column_type,
                nullable=server_default is None,
                server_default=server_default,
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("user_purchases") as batch_op:
        batch_op.drop_column("detail_next_retry_at")
        batch_op.drop_column("detail_last_attempt_at")
        batch_op.drop_column("detail_status")
        batch_op.drop_column("detail_attempts")
