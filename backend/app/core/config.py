"""应用配置——唯一配置入口（pydantic-settings，密钥仅环境变量）。

依据：02-概要设计 §6（pydantic-settings 读环境变量/.env）、08-版本锁定。

env_file 说明（测试阶段修正）：只写 `".env"` 是**相对当前工作目录**的，从仓库根目录跑
`pytest backend/tests` 时读不到 backend/.env，密钥回落为占位值 → api_key 解密失败
（InvalidTag，str 为空）→ 案例向量召回静默降级为文本匹配（D8 实测 1 例测试失败）。
故补充一条按包位置锚定的 .env 作为兜底（后写者优先，即 backend/.env 覆盖 cwd 的同名项）。
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ENV = str(Path(__file__).resolve().parents[2] / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", _BACKEND_ENV), env_file_encoding="utf-8",
                                      extra="ignore")

    app_name: str = "aiops-platform"
    debug: bool = False
    demo_enabled: bool = True  # 演示模式（demo 角色一键进入）；正式交付可按需关闭
    demo_chat_enabled: bool = True  # demo 能否用 AI 助手（只读问答）；公开演示站建议置 false 防刷 token
    demo_daily_token_limit: int = 200_000  # demo 角色每日 token 上限（公开演示站防刷；0=不限）
    demo_data_prefixes: str = ""  # 演示数据可见域（逗号分隔的主机名前缀；空=不隔离，见 services/visibility.py）
    llm_daily_token_limit: int = 0  # 全局每日 token 上限（0=不限；FR 非功能 §8.3 的成本闸门）
    log_json: bool = False  # 日志输出 JSON（容器/采集场景开；本地开发关便于阅读）
    cluster_llm_enabled: bool = True  # 收敛第二级 LLM 因果判定；测试环境关闭以免真实调用
    alert_ai_summary_enabled: bool = True  # FR-C6 告警 AI 摘要总开关（测试关闭以免真调 LLM）
    license_enforce: bool = False     # License 门禁开关（源码交付默认关；发布分发时置 true，FR-A4）
    license_grace_days: int = 14      # 无 License 宽限期（避免新装实例一登录就被自己锁死）
    # 通知卡片里的跳转按钮基地址（如 http://<平台地址>:8848）；留空则不出按钮，避免死链
    public_base_url: str = ""

    # 数据保留期（夯实轮补）。默认值偏保守（180d=半年），可以放长
    # 也能按环境覆盖；设 0 = 不自动清理。`alert_his_events` 与 `audit_logs`
    # 这类只增表不主动瘦，是磁盘稳定性的最大风险源。
    alert_his_retention_days: int = 180
    notification_record_retention_days: int = 90
    audit_log_retention_days: int = 90
    chat_session_retention_days: int = 30

    # 对话上下文管理（D87，用户要求"调研通用大模型对话的上下文管理并完善"）。
    # 背景：改前是把整段历史原样发给模型，长会话 prompt 线性增长 → 费钱、变慢、最终撞上下文上限。
    # 现在走 hybrid（滚动摘要 + 最近窗口），四个旋钮：
    chat_context_budget: int = 8000       # **历史**部分的 token 预算（system 与模型输出另算）。
                                          # 估算偏保守（中文≈1 token/字），真实模型上下文远大于此值；
                                          # 调大＝记得更多但每次更贵，调小＝更省但更早"忘事"。
    chat_compact_threshold: int = 1500    # 溢出段超过这么多 token 才触发摘要压缩（避免每轮都压）
    chat_msg_max_chars: int = 4000        # 单条用户消息字符上限（防一段巨文本撑爆窗口）
    chat_max_stored_messages: int = 400   # 单会话最多存多少条（超出从头归档进摘要；长跑稳定性）

    # 数据库：默认指向本机 SeekDB v1.4.0（冒烟实例，端口 2891）；生产经环境变量覆盖
    db_host: str = "127.0.0.1"
    db_port: int = 2891
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "aiops"

    # 安全
    jwt_secret: str = "change-me-in-env"
    # P5c（FR-411）：自愈全局熔断开关。**运行时可变**（大故障时一键停所有自动恢复），
    # 也可用环境变量 HEAL_KILL_SWITCH=true 在启动时直接关闭（应急处置：先停自愈再启服务）。
    heal_kill_switch: bool = False
    jwt_expire_hours: int = 8
    key_store_secret: str = "change-me-in-env"  # api_key AES-GCM 加密密钥

    # LLM 兜底配置（无 DB 配置时使用；正式部署应在设置页配 LLM 并落库加密存储）
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model_main: str = ""   # 强模型：诊断/RCA/收敛
    llm_model_fast: str = ""   # 快模型：摘要/巡检/命名
    llm_extra: dict = {}

    @property
    def db_async_url(self) -> str:
        return (
            f"mysql+aiomysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def db_sync_url(self) -> str:
        """Alembic 迁移用同步驱动（04 §1：pymysql）。"""
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
