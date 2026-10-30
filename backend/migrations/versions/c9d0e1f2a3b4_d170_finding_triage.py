"""D170（FR-426）：finding_triage 表——诊断报告的人工分诊

发现分诊闭环：AI 诊断报告（rca_findings，粒度=一次诊断运行）由人工处置——
followup（跟进）/ observing（观察）/ dismissed（忽略+理由）。
同主机同指标的忽略记录用于**自动降噪**：后续同类报告标注"历史已忽略"，不再反复打扰。
"""
from alembic import op
import sqlalchemy as sa

revision = "c9d0e1f2a3b4"
down_revision = "b7c8d9e0f12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "finding_triage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("finding_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("incident_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("host_ident", sa.String(length=64), nullable=False, server_default="", index=True),
        sa.Column("metric", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("action", sa.Enum("followup", "observing", "dismissed", name="triage_action"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_triage_finding", "finding_triage", ["finding_id"])
    op.create_index("idx_triage_key", "finding_triage", ["host_ident", "metric"])


def downgrade() -> None:
    op.drop_table("finding_triage")
