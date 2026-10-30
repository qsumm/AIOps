"""Alembic 环境——同步 URL 从应用配置注入（04 §1：迁移走 pymysql 方言）。"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.models.host import Base  # noqa: F401
import app.models.shell   # noqa: F401 —— users/audit_logs/licenses
import app.models.llm     # noqa: F401 —— llm_configs/llm_usage
import app.models.collect # noqa: F401 —— probes + hosts.agent_token
import app.models.alert    # noqa: F401 —— D2 告警域六表
import app.models.prom     # noqa: F401 —— D3 Prometheus 数据源
import app.models.event    # noqa: F401 —— D4 事件与 AI 域
import app.models.ops      # noqa: F401 —— D5 巡检/处置/技能
import app.models.metric    # noqa: F401 —— D154 带维度指标序列(metric_series)
import app.models.notify_rule  # noqa: F401 —— D154 通知路由四表

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().db_sync_url)
target_metadata = Base.metadata

# 非 ORM 管理的表（D1 用 raw SQL 建的 metric_agg，04 §6）不参与 autogenerate 比对
_SKIP_TABLES = {"metric_agg"}
# case_cards 含 VECTOR 列：反射会抛 NullType() takes no arguments（D0 冒烟实测），
# 该表完全由 raw SQL 管理（建表在迁移手写、读写与向量检索在 services/cases.py），
# 故连反射都要跳过——否则任何一次 autogenerate 都会崩（04 §5.1/§10）。
_NO_REFLECT = {"case_cards"}


def _include_name(name, type_, parent_names):
    if type_ == "table" and name in _NO_REFLECT:
        return False
    return True


def _include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in _SKIP_TABLES:
        return False
    # MySQL 把唯一约束实现为唯一索引，反射后与 metadata 的 UniqueConstraint 无法配对，
    # 会误报 added/removed —— 唯一性在建表迁移里由内联约束保证，这里不做比对（04 §6）
    if type_ == "unique_constraint":
        return False
    # SeekDB 的 SHOW CREATE TABLE 输出 `KEY ... BLOCK_SIZE 16384 LOCAL`，
    # SQLAlchemy MySQL 方言解析失败 → 已存在索引被整条丢弃并误报为 added（04 §7）。
    # 因此索引不参与 autogenerate，一律在迁移里手写（建表时内联 sa.Index）。
    if type_ == "index":
        return False
    return True


def _configure(**kw):
    return dict(
        target_metadata=target_metadata,
        compare_type=False,          # 类型变更走人工评审（枚举/长度调整显式写）
        compare_server_default=False,
        include_object=_include_object,
        include_name=_include_name,
        **kw,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_configure(),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"init_command": "SET time_zone = '+00:00'"},  # 与应用一致：会话 UTC
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, **_configure())
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
