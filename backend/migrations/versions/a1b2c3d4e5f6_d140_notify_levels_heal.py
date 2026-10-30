"""D140：告警分级通知渠道 + 自动自愈策略（用户需求）。

需求 1（分级通知）：S1 紧急必须电话，S2/S3 走 IM（企微/钉钉/飞书/微信群）。
  → alert_rules 加 channels_by_level（JSON：{"1":["电话渠道名"],"2":[...],"3":[...]}，
    键为字符串化的 severity）。为空/缺键时回退既有 channels_json（完全向后兼容）。
  → notification_channels 支持 type="phone"（电话，apprise 逃生口）与 "wecom"（企业微信群机器人）。

需求 2（自动自愈）：「指标越线 → 匹配策略 → 执行预定义动作」。
  → heal_policies：策略表（触发条件 + 动作 + 档位 + 冷却）。
    动作复用 heal.ACTIONS 白名单（restart_service / restart_container / reconnect_pool /
    rotate_logs）——不新增自由文本动作，护栏照走。
  → heal_policy_runs：每次触发/执行的留痕（dry_run / waiting_confirm / dispatched /
    denied / failed + 任务 id + 详情），「自愈不是黑盒」。

Revision ID: a1b2c3d4e5f6
Revises: e5f7a9c1d3b5
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "e5f7a9c1d3b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 需求 1：分级通知渠道 ──
    op.add_column("alert_rules",
                  sa.Column("channels_by_level", sa.JSON(), nullable=True))

    # ── 需求 2：自愈策略 ──
    op.create_table(
        "heal_policies",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False, server_default=""),
        # 触发条件：rule_id（关联告警规则，引擎触发时带入）或 metric 直填
        sa.Column("trigger_rule_id", sa.BigInteger(), nullable=True),
        sa.Column("trigger_metric", sa.String(64), nullable=False, server_default=""),
        sa.Column("trigger_op", sa.String(8), nullable=False, server_default=">="),   # >= / <=
        sa.Column("trigger_threshold", sa.Float(), nullable=False, server_default="90"),
        sa.Column("trigger_severity", sa.Integer(), nullable=False, server_default="1"),  # 1/2/3
        # 动作：heal.ACTIONS 白名单枚举 + 目标服务（动作作用于该服务声明的 unit/container）
        sa.Column("action", sa.String(32), nullable=False, server_default="restart_service"),
        sa.Column("service_id", sa.BigInteger(), nullable=True),   # 目标服务；空=告警主机上自动解析
        # 三档语义（与 svc heal 一致）：suggest=只评估留痕 / manual=需人工确认 / auto=护栏通过即执行
        sa.Column("mode", sa.String(16), nullable=False, server_default="suggest"),
        sa.Column("cooldown_min", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_fired_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(24), nullable=False, server_default="idle"),
        # idle / dry_run / waiting_confirm / dispatched / denied / failed / cooldown
        sa.Column("last_detail", sa.JSON(), default=dict),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("idx_heal_pol_rule", "heal_policies", ["trigger_rule_id"])
    op.create_index("idx_heal_pol_enabled", "heal_policies", ["enabled"])

    op.create_table(
        "heal_policy_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("policy_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("alert_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("host_ident", sa.String(64), nullable=False, server_default=""),
        sa.Column("metric", sa.String(64), nullable=False, server_default=""),
        sa.Column("metric_value", sa.Float(), nullable=True),
        sa.Column("action", sa.String(32), nullable=False, server_default=""),
        sa.Column("mode", sa.String(16), nullable=False, server_default="suggest"),
        # dry_run / waiting_confirm / dispatched / denied / failed
        sa.Column("status", sa.String(24), nullable=False, server_default="dry_run"),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("guard_rule", sa.String(64), nullable=False, server_default=""),
        # 注意：TEXT/JSON 列不能带字面 server_default（MySQL/OceanBase 1067 "Invalid default
        # value"——SeekDB 实测）。模型的 default="" 是 python 侧默认，插入时永远有值。
        sa.Column("guard_reason", sa.Text(), nullable=False),
        sa.Column("detail", sa.JSON(), default=dict),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("idx_hpr_policy", "heal_policy_runs", ["policy_id"])
    op.create_index("idx_hpr_time", "heal_policy_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_hpr_time", table_name="heal_policy_runs")
    op.drop_index("idx_hpr_policy", table_name="heal_policy_runs")
    op.drop_table("heal_policy_runs")
    op.drop_index("idx_heal_pol_enabled", table_name="heal_policies")
    op.drop_index("idx_heal_pol_rule", table_name="heal_policies")
    op.drop_table("heal_policies")
    op.drop_column("alert_rules", "channels_by_level")
