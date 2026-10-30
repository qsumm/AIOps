"""健康诊断报告表（D119，FR-C3）。"""
from alembic import op
import sqlalchemy as sa

revision = "a9b3c7d5e1f2"
down_revision = "f0c1a2d3b4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "health_reports",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("host_ident", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DATETIME, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DATETIME, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("source", sa.String(16), nullable=False, server_default="rule"),
        sa.Column("window_start", sa.DATETIME, nullable=True),
        sa.Column("window_end", sa.DATETIME, nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("items", sa.JSON(), nullable=True),
    )
    op.create_index("idx_health_host_time", "health_reports", ["host_ident", "started_at"])


def downgrade() -> None:
    op.drop_index("idx_health_host_time", table_name="health_reports")
    op.drop_table("health_reports")