"""D163-3：llm_configs 增加 fallback_enabled（**用户显式指定**的降级通道）

背景（用户明确纠正，原话）：
  "降级走这个模型 …mimo-v2.6-flash…  用户没有配 qwen3.8-flash。
   你不要替用户做主，万一 qwen3.8-flash 用户不允许用呢"

原实现降级时**隐式**改用同一配置的 `fast_model`（公网是 qwen3.8-flash）——
那是"轻量任务用的快模型"，用户配它**不等于**允许它在主模型额度耗尽时兜底
（配额、合规、计费口径都可能不允许）。这是平台替用户做主。

修法：
  1. 本迁移给 `llm_configs` 加 `fallback_enabled`（默认 0 = 不作为降级通道）；
  2. `pick_fallback_config()` 只取勾了这个标记的配置；一个都没配 → 如实报错
     （LLM_FALLBACK_UNCONFIGURED），由用户去配，而不是擅自用某个模型顶上；
  3. 降级通道是**独立的一条配置**（可与主模型不同服务商 / 不同 base_url / 不同 key /
     不同模型），降级路径直接用它的 base_url + key + model。

不加外键（本项目全库无 FK，靠应用层约束）。
不改已有行的取值：历史配置一律 0（=不作为降级通道）——**保持"没配就是没配"**，
不替用户推断哪个模型可以用。
"""
from alembic import op
import sqlalchemy as sa

revision = "b7c8d9e0f12"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_configs",
        sa.Column("fallback_enabled", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("llm_configs", "fallback_enabled")
