"""AI 诊断评测域模型（FR-428；04 §16）。

设计约束（ADR-005 / 15 号设计方案）：
  ① case 双池是硬约束：`pool ∈ {tune, regress}`，回归池永不参与技能/提示词调优——
     服务层（services/eval.py）读 case 时按池过滤，守卫 test_d182 锁死这条边界；
  ② 快照（snapshot）自包含一次诊断的全部重放材料：collect_context 上下文 +
     逐工具调用的响应（ExecutionStep 已落库，快照再冗余一份，评测不依赖诊断表存活）；
  ③ 版本指纹（fingerprint）：提示词 + 技能集 + 模型配置 + 代码 commit 的 hash，
     评测结果的对比键——指纹不同才谈得上"改前改后"。
"""
from datetime import datetime

from sqlalchemy import BigInteger, DATETIME, Enum, Float, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.host import Base
from app.models.event import MEDIUMTEXT_V


class EvalCase(Base):
    """评测 case：一次诊断的固化快照 + 期望答案（FR-428c）。"""

    __tablename__ = "eval_cases"
    __table_args__ = (
        Index("ix_evalcases_pool", "pool"),
        Index("ix_evalcases_origin", "origin"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(190), default="")          # 人类可读（主机×故障类型×时间）
    origin: Mapped[str] = mapped_column(String(16), default="inject", nullable=False)   # inject|manual（VARCHAR：SeekDB ENUM 过滤禁忌，d184-fix）
    pool: Mapped[str] = mapped_column(String(16), default="regress", nullable=False)    # tune|regress（回归池永不参与调优，ADR-005）
    incident_id: Mapped[int] = mapped_column(BigInteger, default=0)      # 来源事件（0=已不可考）
    host_ident: Mapped[str] = mapped_column(String(64), default="")
    expected: Mapped[str] = mapped_column(String(64), default="")        # 期望根因（fault_type：process_hang/probe_fail/...）
    expected_note: Mapped[str] = mapped_column(String(255), default="")  # 人工标注时的补充说明
    fingerprint: Mapped[str] = mapped_column(String(64), default="")     # 固化时的版本指纹
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)           # {"context": {...}, "steps": [{tool, params, result}...]}
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
    label_status: Mapped[str] = mapped_column(String(16), default="confirmed")   # pending|confirmed|rejected


class EvalRun(Base):
    """一次评测执行：版本指纹 + 逐 case 结果 + 汇总指标（FR-428d）。"""

    __tablename__ = "eval_runs"
    __table_args__ = (
        Index("ix_evalruns_fingerprint", "fingerprint"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")     # 本次评测的版本指纹
    pool: Mapped[str] = mapped_column(String(16), default="regress")     # 本次跑的池
    total: Mapped[int] = mapped_column(Integer, default=0)
    match_rate: Mapped[float] = mapped_column(Float, default=0.0)        # 根因一致率 0~1
    hard_conflict_rate: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_rate: Mapped[float] = mapped_column(Float, default=0.0)     # 证据完整性
    calibration_rate: Mapped[float] = mapped_column(Float, default=0.0) # 置信度校准（INCONCLUSIVE 判对）
    avg_tokens: Mapped[float] = mapped_column(Float, default=0.0)
    details: Mapped[dict] = mapped_column(JSON, default=list)            # 逐 case：[{case_id, expected, actual, match, hard_conflict, ...}]
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
