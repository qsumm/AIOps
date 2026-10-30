"""告警域模型：rules / mutes / cur_events / his_events / channels / records（04 §2 告警区）。"""
from datetime import datetime

from sqlalchemy import CHAR, BigInteger, Boolean, DATETIME, Enum, Float, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.host import Base


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)  # D11：与渠道/模型配置看齐，避免"同名二改误改一"
    group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scope: Mapped[str] = mapped_column(
        Enum("metric", "probe", "prom", "service", name="rule_scope"), default="metric")
    metric: Mapped[str] = mapped_column(String(64), default="")          # scope=metric/probe 的目标指标或探活名；scope=service 时为服务名（空=全部）
    queries_json: Mapped[list] = mapped_column(JSON, default=list)        # scope=prom: PromQL 查询列表（04 §2 原 DDL 有此项，D2 漏建，D3 对齐）
    datasource_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # scope=prom: 指向 prom_datasources
    expr: Mapped[str] = mapped_column(String(512), default=">90")        # 阈值表达式：op value（04 §2 为 VARCHAR(512)）
    severity: Mapped[int] = mapped_column(Integer, default=2)                     # 1紧急/2警告/3通知
    eval_interval_s: Mapped[int] = mapped_column(Integer, default=15)  # 固定间隔评估（cron_pattern 为空时生效）
    cron_pattern: Mapped[str] = mapped_column(String(64), default="")  # 非空则按 cron 评估（FR-C1：'评估间隔（秒）或 cron'）
    window_s: Mapped[int] = mapped_column(Integer, default=300)                   # 评估取数窗口
    for_duration_s: Mapped[int] = mapped_column(Integer, default=0)
    effective_time: Mapped[list] = mapped_column(JSON, default=list)     # [{days:[..], start:"09:00", end:"18:00"}]，空=全天
    algorithm: Mapped[str] = mapped_column(
        Enum("threshold", "3sigma", "isolation_forest", name="detect_algo"), default="threshold"
    )
    notify_recovered: Mapped[bool] = mapped_column(default=True)
    notify_repeat_step_min: Mapped[int] = mapped_column(Integer, default=0)       # 0=只发一次
    notify_max_number: Mapped[int] = mapped_column(Integer, default=0)            # 0=不限
    recover_duration_s: Mapped[int] = mapped_column(Integer, default=300)         # 恢复留观
    channels_json: Mapped[list] = mapped_column(JSON, default=list)      # ["feishu","webhook"] 渠道名
    # D140 分级通知：{"1":["电话渠道"],"2":[...],"3":[...]}（键=字符串化 severity）。
    # 非空且含对应级别键时优先于 channels_json；S1（紧急）必须含电话类渠道（写入时校验）。
    channels_by_level: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    append_tags: Mapped[dict] = mapped_column(JSON, default=dict)
    # D154（FR-423）：告警规则级事件 Pipeline —— 在**事件产生时**执行（算指纹之前）：
    # 改标签（→ 进指纹 → 不同维度成独立事件）、富化上下文、或 drop（不产生事件）。
    # 位置与语义对齐夜莺的 alert_rule.event_relabel_config（见 ADR-003）。
    event_pipelines: Mapped[list] = mapped_column(JSON, default=list)
    enable_ai_summary: Mapped[bool] = mapped_column(default=True)
    runbook_url: Mapped[str] = mapped_column(String(255), default="")
    disabled: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now(), onupdate=func.now())


class AlertMute(Base):
    __tablename__ = "alert_mutes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cause: Mapped[str] = mapped_column(String(255), default="")
    tag_filters: Mapped[list] = mapped_column(JSON, default=list)   # [{k:"host",op:"==",v:"x"}] op:==/!=/=/~/in
    severities: Mapped[list] = mapped_column(JSON, default=list)    # [1,2]；空=全部
    btime: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    etime: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    periodic_mutes: Mapped[list] = mapped_column(JSON, default=list)  # [{start:"02:00", end:"03:00", days:[..]}]
    mute_type: Mapped[int] = mapped_column(Integer, default=0)                # 0全屏蔽 1只屏蔽通知
    disabled: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DATETIME, nullable=True, onupdate=func.now())   # D171：配置可编辑，变更需可追溯


class AlertCurEvent(Base):
    __tablename__ = "alert_cur_events"
    # D2 实现注：hash 为主键（04）；labels 冗余 host/metric 便于检索
    hash: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    rule_id: Mapped[int] = mapped_column(BigInteger, index=True)
    rule_name: Mapped[str] = mapped_column(String(128), default="")
    rule_config: Mapped[dict] = mapped_column(JSON, default=dict)
    host_ident: Mapped[str] = mapped_column(CHAR(64), default="", index=True)
    metric: Mapped[str] = mapped_column(String(64), default="")
    severity: Mapped[int] = mapped_column(Integer, default=2)
    trigger_time: Mapped[datetime] = mapped_column(DATETIME, default=lambda: datetime.utcnow())
    trigger_value: Mapped[float] = mapped_column(Float, default=0.0)
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
    # D154：注解（只给人看的上下文，不参与屏蔽/路由匹配；标签才参与）
    annotations: Mapped[dict] = mapped_column(JSON, default=dict)
    first_trigger_time: Mapped[datetime] = mapped_column(DATETIME, default=lambda: datetime.utcnow())
    notify_cur_number: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(Enum("firing", "ack", name="event_status"), default="firing")
    ai_summary: Mapped[str] = mapped_column(Text, default="")
    # P0：新鲜度。规则停用/删除、平台重启、数据源不可达时，活跃行会滞留；
    # stale=True 后不再参与通知/聚类/诊断，界面显示“数据陈旧”。
    last_eval_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    stale_reason: Mapped[str] = mapped_column(String(64), default="", server_default="")


class AlertHisEvent(Base):
    __tablename__ = "alert_his_events"
    __table_args__ = (Index("idx_his_query", "host_ident", "trigger_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hash: Mapped[str] = mapped_column(CHAR(32), index=True)
    rule_id: Mapped[int] = mapped_column(BigInteger)
    rule_name: Mapped[str] = mapped_column(String(128), default="")
    host_ident: Mapped[str] = mapped_column(CHAR(64), default="")
    metric: Mapped[str] = mapped_column(String(64), default="")
    severity: Mapped[int] = mapped_column(Integer, default=2)
    trigger_time: Mapped[datetime] = mapped_column(DATETIME)
    trigger_value: Mapped[float] = mapped_column(Float, default=0.0)
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
    annotations: Mapped[dict] = mapped_column(JSON, default=dict)
    is_recovered: Mapped[bool] = mapped_column(default=False)
    recover_time: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    ai_summary: Mapped[str] = mapped_column(Text, default="")


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # 内置 feishu/dingtalk/email/webhook；其它 apprise scheme 需配 apprise_url
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)  # webhook_url / smtp{host,port,user,pass,to}
    enabled: Mapped[bool] = mapped_column(default=True)
    last_test_status: Mapped[str] = mapped_column(Enum("ok", "failed", "unknown", name="test_status"), default="unknown")
    # 通知熔断（夯实轮补）：坏渠道（URL 写错/服务端拒绝）此前每次告警都重试，
    # 实测几分钟内累积 33 条失败台账。连续失败达阈值 → 冷却期内跳过，避免无效投递与台账膨胀。
    fail_streak: Mapped[int] = mapped_column(Integer, default=0)
    disabled_until: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DATETIME, nullable=True, onupdate=func.now())   # D171：配置可编辑，变更需可追溯


class NotificationRecord(Base):
    __tablename__ = "notification_records"
    __table_args__ = (Index("idx_notif_time", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_hash: Mapped[str] = mapped_column(CHAR(32), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="")
    target: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(Enum("sent", "failed", name="notif_status"), default="sent")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
