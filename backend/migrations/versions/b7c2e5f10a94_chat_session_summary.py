"""ai_chat_sessions 增加滚动摘要（对话上下文管理，D87）

Revision ID: b7c2e5f10a94
Revises: d91b47f2c8a5

为什么要这两列：改前对话把**整段历史**原样发给模型，长会话下 prompt 线性增长（费钱、变慢、
最终撞上下文上限直接报错）。改成 hybrid 记忆（滚动摘要 + 最近窗口）后，需要把"更早的对话压成的
摘要"持久化下来，以及记录**摘要覆盖到第几条消息**：

- `summary`      ：滚动摘要正文（fast 模型生成；失败时如实写入"未能压缩"提示）
- `summary_upto` ：messages 数组的下标——`messages[:summary_upto]` 已折进摘要
  （`messages` 本身**展示用全量保留**，用户回看历史不会变短；发给模型的只是 `messages[summary_upto:]`）
"""
from alembic import op

revision = "b7c2e5f10a94"
down_revision = "d91b47f2c8a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai_chat_sessions ADD COLUMN summary TEXT NULL")
    op.execute("ALTER TABLE ai_chat_sessions ADD COLUMN summary_upto INT NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE ai_chat_sessions DROP COLUMN summary")
    op.execute("ALTER TABLE ai_chat_sessions DROP COLUMN summary_upto")
