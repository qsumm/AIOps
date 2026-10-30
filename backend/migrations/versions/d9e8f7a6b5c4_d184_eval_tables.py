"""D184：AI 诊断评测闭环两表（FR-428；04 §16）。

eval_cases / eval_runs —— 见 app/models/eval.py 模块注释与 15 号设计方案。
索引手写（SeekDB 反射不支持 BLOCK_SIZE，04 §7 纪律）。
"""
revision = "d9e8f7a6b5c4"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.create_table(
        "eval_cases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=190), server_default="", nullable=False),
        sa.Column("origin", sa.Enum("inject", "manual", name="eval_origin"), nullable=False),
        sa.Column("pool", sa.Enum("tune", "regress", name="eval_pool"), nullable=False),
        sa.Column("incident_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("host_ident", sa.String(length=64), server_default="", nullable=False),
        sa.Column("expected", sa.String(length=64), server_default="", nullable=False),
        sa.Column("expected_note", sa.String(length=255), server_default="", nullable=False),
        sa.Column("fingerprint", sa.String(length=64), server_default="", nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DATETIME(), server_default=sa.func.now(), nullable=False),
        sa.Column("label_status", sa.Enum("pending", "confirmed", "rejected", name="eval_label_status"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_evalcases_pool", "eval_cases", ["pool"])
    op.create_index("ix_evalcases_origin", "eval_cases", ["origin"])

    op.create_table(
        "eval_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("fingerprint", sa.String(length=64), server_default="", nullable=False),
        sa.Column("pool", sa.String(length=16), server_default="regress", nullable=False),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("match_rate", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("hard_conflict_rate", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("evidence_rate", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("calibration_rate", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("avg_tokens", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DATETIME(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_evalruns_fingerprint", "eval_runs", ["fingerprint"])


def downgrade() -> None:
    op.drop_index("ix_evalruns_fingerprint", "eval_runs")
    op.drop_table("eval_runs")
    op.drop_index("ix_evalcases_origin", "eval_cases")
    op.drop_index("ix_evalcases_pool", "eval_cases")
    op.drop_table("eval_cases")
