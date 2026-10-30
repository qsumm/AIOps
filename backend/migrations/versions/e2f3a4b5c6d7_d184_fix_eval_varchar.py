"""D184-fix：eval 表三个 ENUM 列放开为 VARCHAR（SeekDB 禁忌：ENUM 不做过滤列）。

实测：`WHERE pool='regress'`（raw SQL 同样）报 `(1210, 'Invalid argument')`——
与 D8-fix（b7c4e1a95d20）同族的 SeekDB ENUM 过滤缺陷。先例做法：列放开
VARCHAR，合法性由应用层校验（模型 default + 服务层显式检查已有）。
"""
revision = "e2f3a4b5c6d7"
down_revision = "d9e8f7a6b5c4"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.alter_column("eval_cases", "origin",
                    existing_type=sa.Enum("inject", "manual"),
                    type_=sa.String(length=16), existing_nullable=False)
    op.alter_column("eval_cases", "pool",
                    existing_type=sa.Enum("tune", "regress"),
                    type_=sa.String(length=16), existing_nullable=False)
    op.alter_column("eval_cases", "label_status",
                    existing_type=sa.Enum("pending", "confirmed", "rejected"),
                    type_=sa.String(length=16), existing_nullable=False)


def downgrade() -> None:
    op.alter_column("eval_cases", "label_status",
                    existing_type=sa.String(length=16),
                    type_=sa.Enum("pending", "confirmed", "rejected"), existing_nullable=False)
    op.alter_column("eval_cases", "pool",
                    existing_type=sa.String(length=16),
                    type_=sa.Enum("tune", "regress"), existing_nullable=False)
    op.alter_column("eval_cases", "origin",
                    existing_type=sa.String(length=16),
                    type_=sa.Enum("inject", "manual"), existing_nullable=False)
