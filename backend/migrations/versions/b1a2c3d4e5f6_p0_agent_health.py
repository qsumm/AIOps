"""P0：Agent 认证自愈与自监控（Host 健康字段）

背景：
  · Agent 运行中收到 401 后原先静默重试，平台只看到 last_beat 变旧，无法区分
    “机器关机”和“凭证失效”；
  · 主机详情没有认证状态、最近成功上报、缓存积压、最近错误，排障只能翻 agent.log。

本迁移只加字段，不改状态枚举：
  auth_state          ok / auth_failed / unknown
  last_auth_ok_at     最近一次认证成功（注册/心跳/上报）
  last_report_ok_at   最近一次成功收到上报（平台时间，不受客户端时钟偏差影响）
  cache_depth         断网缓存待传条数（Agent 下一次上报携带）
  last_agent_error    Agent 最近一次错误摘要（截断 255）
"""
from alembic import op
import sqlalchemy as sa

revision = "b1a2c3d4e5f6"
down_revision = "c8a1f2b9d643"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("auth_state", sa.String(16), nullable=False, server_default="unknown"))
    op.add_column("hosts", sa.Column("last_auth_ok_at", sa.DateTime(), nullable=True))
    op.add_column("hosts", sa.Column("last_report_ok_at", sa.DateTime(), nullable=True))
    op.add_column("hosts", sa.Column("cache_depth", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("hosts", sa.Column("last_agent_error", sa.String(255), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("hosts", "last_agent_error")
    op.drop_column("hosts", "cache_depth")
    op.drop_column("hosts", "last_report_ok_at")
    op.drop_column("hosts", "last_auth_ok_at")
    op.drop_column("hosts", "auth_state")
