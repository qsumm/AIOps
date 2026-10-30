"""D173（技能体系升级 P2/P3）：skills 增 commands 列 + source 枚举加 imported

- `commands`：技能授权的**只读命令**（会话作用域白名单扩展，平台与 Agent 双侧校验）；
  JSON 列，语义同 alerts_scope（列表）。
- `source` 枚举加 'imported'：P3 从 GitHub 导入的技能与"用户自建"要能区分开
  （来源可追溯，交付规范要求）。MySQL 5.7 改枚举用 MODIFY COLUMN。
"""
from alembic import op
import sqlalchemy as sa

revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills", sa.Column("commands", sa.JSON(), nullable=True))
    op.execute("ALTER TABLE skills MODIFY COLUMN source "
               "ENUM('builtin','user','imported') NOT NULL DEFAULT 'builtin'")


def downgrade() -> None:
    op.execute("UPDATE skills SET source='user' WHERE source='imported'")
    op.execute("ALTER TABLE skills MODIFY COLUMN source ENUM('builtin','user') NOT NULL DEFAULT 'builtin'")
    op.drop_column("skills", "commands")
