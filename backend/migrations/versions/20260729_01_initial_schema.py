"""Create Librovia's initial schema without overwriting existing databases."""

from alembic import op
import sqlalchemy as sa

revision = "20260729_01"
down_revision = None
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _create_index(name: str, table: str, columns: list[str]) -> None:
    if name not in _index_names(table):
        op.create_index(name, table, columns, unique=False)


def upgrade() -> None:
    tables = _table_names()
    if "sttandard_books" not in tables:
        op.create_table(
            "sttandard_books",
            sa.Column("isbn", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("author", sa.String(), nullable=True),
            sa.Column("cover_url", sa.String(), nullable=True),
            sa.Column("category", sa.String(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("isbn"),
        )
    _create_index("ix_sttandard_books_title", "sttandard_books", ["title"])

    if "user_purchases" not in tables:
        op.create_table(
            "user_purchases",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("platform", sa.String(), nullable=False),
            sa.Column("platform_book_id", sa.String(), nullable=True),
            sa.Column("isbn", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["isbn"], ["sttandard_books.isbn"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index("ix_user_purchases_user_id", "user_purchases", ["user_id"])
    _create_index("ix_user_purchases_isbn", "user_purchases", ["isbn"])

    if "user_wishlist" not in tables:
        op.create_table(
            "user_wishlist",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("platform", sa.String(), nullable=False),
            sa.Column("sync_status", sa.String(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("isbn", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["isbn"], ["sttandard_books.isbn"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index("ix_user_wishlist_user_id", "user_wishlist", ["user_id"])
    _create_index("ix_user_wishlist_isbn", "user_wishlist", ["isbn"])

    if "platform_sessions" not in tables:
        op.create_table(
            "platform_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("platform", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index("ix_platform_sessions_user_id", "platform_sessions", ["user_id"])


def downgrade() -> None:
    raise RuntimeError(
        "The baseline migration cannot be downgraded safely; restore a backup instead"
    )
