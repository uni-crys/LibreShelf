"""Add durable metadata enrichment jobs."""

from alembic import op
import sqlalchemy as sa

revision = "20260729_02"
down_revision = "20260729_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "metadata_jobs" not in inspector.get_table_names():
        op.create_table(
            "metadata_jobs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("platform", sa.String(), nullable=False),
            sa.Column("platform_book_id", sa.String(), nullable=False),
            sa.Column("raw_identifier", sa.String(), nullable=False),
            sa.Column("raw_title", sa.String(), nullable=False),
            sa.Column("crawler_cover", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("result", sa.String(), nullable=True),
            sa.Column("last_error_type", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "platform",
                "platform_book_id",
                name="uq_metadata_job_platform_book",
            ),
        )
    index_names = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("metadata_jobs")
    }
    for name, columns in (
        ("ix_metadata_jobs_user_id", ["user_id"]),
        ("ix_metadata_jobs_platform", ["platform"]),
        ("ix_metadata_jobs_status", ["status"]),
    ):
        if name not in index_names:
            op.create_index(name, "metadata_jobs", columns, unique=False)


def downgrade() -> None:
    op.drop_index("ix_metadata_jobs_status", table_name="metadata_jobs")
    op.drop_index("ix_metadata_jobs_platform", table_name="metadata_jobs")
    op.drop_index("ix_metadata_jobs_user_id", table_name="metadata_jobs")
    op.drop_table("metadata_jobs")
