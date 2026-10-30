"""Prometheus 数据源模型（FR-B3 轨 B；03 §1.6）。

04 §2 的 23 表清单未定义该表（03 §1.6 已引用 `datasource.base_url` 与 Basic/Bearer 认证），
属设计缺口，D3 补齐为第 24 张表并回写 04 §8。
凭据（密码/Token）复用 key_store_secret 做 AES-GCM 加密（同 llm_configs.api_key）。
"""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DATETIME, Enum, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.host import Base


class PromDatasource(Base):
    __tablename__ = "prom_datasources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)   # 例 http://127.0.0.1:9090
    auth_type: Mapped[str] = mapped_column(Enum("none", "basic", "bearer", name="prom_auth"), default="none")
    username: Mapped[str] = mapped_column(String(64), default="")        # auth_type=basic
    secret_encrypted: Mapped[str] = mapped_column(String(512), default="")  # basic 密码 / bearer token（AES-GCM）
    timeout_s: Mapped[int] = mapped_column(Integer, default=10)
    # D157：mode=scrape 的拉取间隔（秒）—— 0=不自动拉（只能手动试采）
    # 默认 30s 与现有 sched.py 的全局硬编码一致；用户可按 exporter 暴露开销调
    scrape_interval_s: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(default=True)
    is_default: Mapped[bool] = mapped_column(default=False)
    # 连通性自检（连接测试按钮写入；连续失败另由规则侧 eval_error 计数触发平台自监控告警）
    # FR-B3+：mode=query 走 Prom API 查询代理；mode=scrape 直接拉 exporter 的 /metrics
    mode: Mapped[str] = mapped_column(String(16), default="query")
    scrape_config: Mapped[dict] = mapped_column(JSON, default=dict)  # {path:"/metrics", metric_keep:["node_..."]}
    last_check_status: Mapped[str] = mapped_column(
        Enum("ok", "failed", "unknown", name="prom_check"), default="unknown"
    )
    last_check_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, server_default=func.now())
