"""通知路由相关模型（FR-423；D154）。

四张表：
- `notify_rules`        通知规则（一条规则 = 名字 + 若干条通知配置 + 规则级 Pipeline）
- `alert_subscribes`   订阅规则（别的团队自己订阅，**不改**被订阅的规则）
- `busi_groups`        业务组（主机归属 + 路由标签；**本轮不做权限边界**）
- `busi_group_members` 业务组成员（团队级 + 读写标记）

设计取舍（详见 ADR-003）：
- 通知配置用 **JSON** 承载（一条规则内多个配置的筛选维度各不相同，拆表会变成多表 join，
  收益低于 JSON 的可读性代价）——与夜莺 `models/notify_rule.go` 的 `NotifyConfigs` 同构；
- 业务组本轮只做**路由标签**（主机归属 + 通知规则按组筛），**不作为数据可见性权限边界**；
- 老的 `AlertRule.channels_by_level`（按级别选渠道名）**完全保留**，两套并存且互斥
  （新规则命中即接管该事件，否则回落老路径）——见 services/notify_rules.py 的 resolve_targets。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.host import Base


class NotifyRule(Base):
    """通知规则：决定「什么事件、走什么媒介、发给谁」。"""

    __tablename__ = "notify_rules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    busi_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # NotifyConfig[]：{"channel_id","template_id","severities","time_ranges",
    #                  "label_keys","attributes","targets","pipeline_id"}
    configs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 规则级 Pipeline[]（在屏蔽之前跑，影响所有下游）
    pipelines: Mapped[list] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=True)

    __table_args__ = (Index("idx_nrule_enabled", "enabled"),)


class AlertSubscribe(Base):
    """订阅规则：订阅方自己配，**不需要**被订阅方改规则。"""

    __tablename__ = "alert_subscribes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(BigInteger, nullable=False)      # 订阅哪条告警规则
    busi_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 谁订阅
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    # 筛选条件（与通知配置同语义）：{"severities":[],"time_ranges":[],"label_keys":[],"attributes":[]}
    severity_filter: Mapped[dict] = mapped_column(JSON, nullable=True)
    # 订阅方自己的投递配置：{"channel_id","template_id","targets"}
    config: Mapped[dict] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=True)

    __table_args__ = (Index("idx_sub_rule", "rule_id"),)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, onupdate=func.now())   # D171：配置可编辑，变更需可追溯


class BusiGroup(Base):
    """业务组：主机归属 + 路由标签（本轮不做数据可见性权限边界，见 ADR-003）。"""

    __tablename__ = "busi_groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 名字带层级（形如 DBA/MySQL），界面按分隔符渲染成树（夜莺同做法）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, onupdate=func.now())   # D171：配置可编辑，变更需可追溯


class BusiGroupMember(Base):
    """业务组成员：**团队级**授权（不是用户级），读写二选一。"""

    __tablename__ = "busi_group_members"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    busi_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    team: Mapped[str] = mapped_column(String(64), nullable=False)
    permission: Mapped[str] = mapped_column(
        Enum("read", "write", name="busi_permission"), nullable=False, default="read", server_default="read")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=True)

    __table_args__ = (Index("idx_bgm_group", "busi_group_id", "team", unique=True),)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, onupdate=func.now())   # D171：配置可编辑，变更需可追溯
