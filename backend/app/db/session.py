"""数据库会话与引擎（dal 层唯一入口）。

依据：02-概要设计 §1.2；04 §1（aiomysql 方言）。
engine 按事件循环惰性创建：生产（uvicorn 单循环）= 单引擎；测试多循环各自独立。
用法：`async with SessionLocal() as db:`；FastAPI 依赖用 `Depends(get_db)`。
"""
import asyncio
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

_engines: dict[int, tuple] = {}


def _get_engine_and_maker():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    key = id(loop) if loop else 0
    if key not in _engines:
        eng = create_async_engine(
            settings.db_async_url,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=False,
            # 强制会话时区为 UTC：SeekDB 默认取服务端本地时区，会让 server_default=now()
            # 比应用侧 datetime.utcnow() 快若干小时（D4 实测：前端出现 "-27896 秒前"）。
            # 全库时间戳统一 UTC（04 §1）由此在连接层保证。
            connect_args={
                "init_command": "SET time_zone = '+00:00'",
                # 挂库容错（夯实轮实测）：DB 挂起（网络分区/存储卡死）时，没有读超时的
                # 查询会**无限期挂住**——所有 API 同时 hang 到客户端自己超时，引擎评估
                # 循环也被拖死。建连超时让挂起在有限时间内变成 OperationalError →
                # 统一错误信封（500），DB 恢复后 pool_pre_ping 自愈（均已实测）。
                # 读/写超时 aiomysql 暂不支持作为 connect kwargs，留在 engine 上
                # 用 pool 的 connect_args={"connect_timeout": 5} 与语句级 MAX_EXECUTION_TIME。
                "connect_timeout": 5,
            },
            # MySQL 服务端语句执行上限（秒）：命中后会返回 3024 错误 → 走统一错误信封
            # 而不是无限挂住。覆盖 99% 的正常查询（系统最重查询远低于此）。
            execution_options={"max_execution_time": 30000},
        )
        _engines[key] = (eng, async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False))
    return _engines[key]


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每请求一会话。"""
    _, maker = _get_engine_and_maker()
    async with maker() as session:
        yield session


def SessionLocal() -> AsyncSession:
    """调度器/后台/测试直取会话：`async with SessionLocal() as db:`。"""
    _, maker = _get_engine_and_maker()
    return maker()


async def dispose_all_engines() -> None:
    for eng, _ in _engines.values():
        await eng.dispose()
    _engines.clear()
