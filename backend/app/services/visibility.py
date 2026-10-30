"""数据可见域（FR-A1 的"演示模式数据隔离"）。

需求原文：演示访客「只读演示数据，一键进入免注册」，验收是"演示模式一键进入且数据隔离
（仅预置演示数据）"。

诚实说明：本平台是**单租户自托管**产品，绝大多数部署里"库里的数据就是这台机器自己的数据"，
演示账号看到的就是演示数据，隔离天然成立。真正需要过滤的是**混合场景**——
一台公开演示机同时挂着演示数据和（比如）运营自己的真实主机。

因此实现取最小可用形态：**按主机名前缀划分可见域**
- `DEMO_DATA_PREFIXES`（逗号分隔，**默认为空 = 不隔离**，行为与从前完全一致）；
- 非空时：demo 角色只能看到名字命中这些前缀的主机，以及**与这些主机相关**的告警/事件/指标；
- admin/member 不受影响；
- 不做行级多租户（那是另一套设计：租户列 + 全表过滤 + 越权测试，V1 明确不做）。

设计取舍：用"名单前缀"而不是新增 `tenant_id` 列，是因为前者零迁移、零数据改造、
出问题只需把配置清空即可回滚。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.models.host import Host
from app.models.shell import User


def demo_prefixes() -> list[str]:
    raw = (get_settings().demo_data_prefixes or "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def restricted(user: User | None) -> bool:
    """该用户是否需要按可见域过滤。"""
    return bool(user is not None and user.role == "demo" and demo_prefixes())


async def visible_host_idents(db, user: User | None) -> set[str] | None:
    """返回该用户可见的主机 ident 集合；None 表示不限制（admin/member 或未配置）。"""
    if not restricted(user):
        return None
    rows = (await db.execute(select(Host.ident, Host.name))).all()
    pre = tuple(demo_prefixes())
    return {ident for ident, name in rows if (name or "").startswith(pre)}


async def visible_fingerprints_filter(db, user: User | None, column):
    """给"按 host_ident 过滤"的查询用：返回可绑定到 where 的条件；None 表示不过滤。"""
    idents = await visible_host_idents(db, user)
    if idents is None:
        return None
    return column.in_(list(idents) or ["-"])


async def host_visible(db, user: User | None, ident: str) -> bool:
    idents = await visible_host_idents(db, user)
    return True if idents is None else ident in idents


def recent_window(minutes: int = 15) -> tuple[datetime, datetime]:
    end = datetime.utcnow()
    return end - timedelta(minutes=minutes), end
