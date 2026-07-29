"""Separate wishlist platform product IDs from canonical book IDs."""

from alembic import op
import sqlalchemy as sa

revision = "20260729_03"
down_revision = "20260729_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {
        column["name"]
        for column in inspector.get_columns("user_wishlist")
    }
    if "platform_book_id" not in columns:
        op.add_column(
            "user_wishlist",
            sa.Column("platform_book_id", sa.String(), nullable=True),
        )
        op.execute(
            "UPDATE user_wishlist SET platform_book_id = isbn "
            "WHERE platform_book_id IS NULL"
        )
    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("user_wishlist")
    }
    if "ix_user_wishlist_platform_book_id" not in indexes:
        op.create_index(
            "ix_user_wishlist_platform_book_id",
            "user_wishlist",
            ["platform_book_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index(
        "ix_user_wishlist_platform_book_id",
        table_name="user_wishlist",
    )
    with op.batch_alter_table("user_wishlist") as batch_op:
        batch_op.drop_column("platform_book_id")
