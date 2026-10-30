"""D21：llm_usage 增加 role（用量归属），修「演示站配额被全平台用量吃光」

背景（用户实测报障）：
  demo 用户在新会话问「现在有哪些活跃告警？」→ 429
  `LLM_QUOTA_EXCEEDED：演示模式今日 token 已用尽（760070/200000）`

  限额 20 万，却记了 76 万 —— 因为原实现 `tokens_used_today()` 统计的是
  **全平台当天总量**，不区分是谁产生的：
    · summary 593 次 / 280,993 token —— **后台引擎自动**生成的告警 AI 摘要（没有任何用户触发）
    · rca     53 次 / 423,652 token —— 管理员做验收测试时的 AI 诊断
    · chat / inspect / embed            —— 同样不是 demo 产生的
  demo 用户自己几乎没消耗，却被自己的"防刷额度"挡住。

修法：
  1. 本迁移给 `llm_usage` 加 `role`（用户角色 / "engine" 后台任务）；
  2. `tokens_used_today(db, role)` 支持按角色统计；
  3. `quota_state()` 在 demo 口径下**只统计 demo 自己的用量**（全局限额仍统计全平台）。
  4. `call_llm` 落库时写入 `role`，各处调用的角色由路由透传（chat/diagnose/inspect/recall）。

历史行无法追溯归属，`role` 取默认空串 —— 即不把历史消耗算到 demo 头上（这样修完
demo 立即可用；若算到 demo 头上则是把管理员的账记在用户身上，语义相反）。

注意：**不加外键**（本项目全库无 FK，靠应用层约束）；SeekDB 对 ENUM 索引首列有限制，
故 role 用 VARCHAR 而非 ENUM。
"""
from alembic import op
import sqlalchemy as sa

revision = 'f4b2c8d91e35'
down_revision = 'd3f8a1c47b92'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("role", sa.String(16), nullable=False, server_default=""))
    op.create_index("ix_llm_usage_role", "llm_usage", ["role"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_role", table_name="llm_usage")
    op.drop_column("llm_usage", "role")
