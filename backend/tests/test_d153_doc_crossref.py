"""D153 文档自检：交叉引用可解析 + 引用的源码位置真实存在 + 编号不冲突。

动机：这套文档里有大量 `file:line` 引用与文档间互链。**引用错了就等于杜撰**，
所以用机械方式核一遍：链接指向的文件必须存在；引用的代码行必须真的在文件里。

同时守住两条编号纪律：
  · 新建表的编号不得与 04-数据库设计说明书 已登记的「D3/D4 补录表」冲突；
  · 新增 FR 编号不得与 PRD 既有 FR 冲突。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
N9E = pathlib.Path.home() / "nightingale"

DOCS = [
    "docs/02-调研/夜莺能力对标与三项能力补齐调研-20261021.md",
    "docs/01-需求与设计/13-三项能力补齐（可行性评估与开发方案）.md",
    "docs/01-需求与设计/decisions/ADR-003-标签模型与通知路由.md",
    "docs/01-需求与设计/decisions/ADR-004-采集插件机制与多维存储选型.md",
    # D158 文档轮：核心设计文档也纳入引用校验（当时核过存量 16 处引用全部有效）
    "docs/01-需求与设计/02-概要设计说明书.md",
    "docs/01-需求与设计/03-详细设计说明书.md",
    "docs/01-需求与设计/04-数据库设计说明书.md",
    "docs/01-需求与设计/05-接口设计说明书.md",
    "docs/01-需求与设计/06-测试计划.md",
]


def _check_links(fails: list[str]) -> int:
    """文档间互链必须指向真实文件。"""
    checked = 0
    link_re = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for d in DOCS:
        f = ROOT / d
        if not f.exists():
            fails.append(f"文档不存在：{d}")
            continue
        for _label, target in link_re.findall(f.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            checked += 1
            if not (f.parent / target).resolve().exists():
                fails.append(f"{d}: 链接指向不存在的文件 → {target}")
    return checked


def _check_platform_cites(fails: list[str]) -> int:
    """平台侧 `file:line` 引用：文件存在 + 行号在范围内 + 目标行非空。

    支持三种行号写法：`file.py:12`、`file.py:12-20`、`file.py:74,99`。
    """
    checked = 0
    cite_re = re.compile(
        r"`(backend/app/[\w/]+\.py|agent/agent_main\.py|static/js/[\w/]+\.js)"
        r":(\d+(?:[-,]\d+)*)`")
    for d in DOCS:
        text = (ROOT / d).read_text(encoding="utf-8")
        for rel, spec in cite_re.findall(text):
            p = ROOT / rel
            if not p.exists():
                fails.append(f"{d}: 引用的文件不存在 → {rel}")
                continue
            lines = p.read_text(encoding="utf-8").splitlines()
            for part in spec.split(","):
                if "-" in part:
                    a, b = part.split("-", 1)
                    nums = list(range(int(a), int(b) + 1))
                else:
                    nums = [int(part)]
                for n in nums:
                    checked += 1
                    if n < 1 or n > len(lines):
                        fails.append(f"{d}: 行号越界 → {rel}:{n}（文件共 {len(lines)} 行）")
                    elif not lines[n - 1].strip():
                        fails.append(f"{d}: 引用位置是空行 → {rel}:{n}")
    return checked


def _check_n9e_cites(fails: list[str]) -> int:
    """n9e 侧 `file:line` 引用：本地有源码才核（没克隆则跳过，不误报）。"""
    if not N9E.exists():
        return 0
    checked = 0
    cite_re = re.compile(
        r"`(models/[\w]+\.go|alert/process/process\.go|center/router/router_a2a\.go)"
        r":(\d+(?:[-,]\d+)*)`")
    for d in DOCS:
        text = (ROOT / d).read_text(encoding="utf-8")
        for rel, spec in cite_re.findall(text):
            p = N9E / rel
            if not p.exists():
                fails.append(f"{d}: n9e 引用文件不存在 → {rel}")
                continue
            lines = p.read_text(encoding="utf-8").splitlines()
            for part in spec.split(","):
                if "-" in part:
                    a, b = part.split("-", 1)
                    nums = list(range(int(a), int(b) + 1))
                else:
                    nums = [int(part)]
                for n in nums:
                    checked += 1
                    if n < 1 or n > len(lines):
                        fails.append(f"{d}: n9e 行号越界 → {rel}:{n}（共 {len(lines)} 行）")
    return checked


def test_docs_crossref_resolvable():
    """全部引用必须可解析。"""
    fails: list[str] = []
    checked = _check_links(fails)
    checked += _check_platform_cites(fails)
    checked += _check_n9e_cites(fails)
    assert not fails, "文档引用有误：\n" + "\n".join(fails)
    assert checked > 0, "一处引用都没核到（正则失效了？）"


def test_new_table_numbers_do_not_conflict():
    """表编号与设计文档对齐（D158 文档轮改写为「已登记」口径）。

    设计文档（02 §10.6 / 13）把 26~31 预留给 v1.5 六表：26 `host_collectors` 实施时
    **未建**（FR-422 改走 exporter 直采，编号保留不复用），27~31 五张已建成并在 04 §13
    登记；历史遗留八张从 32 起补登。本测试锁死这套编号的现状：
      · 26 必须空缺（谁挪用了这个编号就是和设计文档对不上）；
      · 27~31 必须已登记；
      · 已登记编号全集必须无重复、无越界（超出 04 §13 声明的 39 个表定义即失败）。
    """
    db_doc = (ROOT / "docs/01-需求与设计/04-数据库设计说明书.md").read_text(encoding="utf-8")
    taken = [int(m) for m in re.findall(r"新增第 (\d+) 张表", db_doc)]
    assert len(taken) == len(set(taken)), f"表编号重复登记：{sorted(taken)}"
    assert 26 not in taken, "第 26 张表 host_collectors 未建，编号必须保留空缺"
    assert {27, 28, 29, 30, 31} <= set(taken), "D154 五张新表（27~31）未在 04 文档登记"
    assert set(taken) == {24, 25, 27, 28, 29, 30, 31, *range(32, 40)}, (
        f"登记编号全集变了：{sorted(taken)}（应为 24/25 + 27~31 + 32~39，共 15 个补录编号）"
    )
    design = (ROOT / "docs/01-需求与设计/13-三项能力补齐（可行性评估与开发方案）.md").read_text(encoding="utf-8")
    for t in ("host_collectors", "notify_rules", "alert_subscribes",
              "busi_groups", "busi_group_members", "metric_series"):
        assert t in design, f"设计文档缺表 {t}"


def test_all_model_tables_registered_in_db_doc():
    """37 张 SQLAlchemy 模型表必须全部在 04 文档有 `表名(` 定义块（D158 新增，防再欠账）。"""
    import pathlib as _pl

    db_doc = (ROOT / "docs/01-需求与设计/04-数据库设计说明书.md").read_text(encoding="utf-8")
    defined = set(re.findall(r"(?m)^([a-z_][a-z0-9_]*)\(", db_doc))
    model_tables = set()
    for fp in (_pl.Path(__file__).resolve().parents[1] / "app" / "models").glob("*.py"):
        text = fp.read_text(encoding="utf-8")
        model_tables.update(re.findall(r'__tablename__\s*=\s*["\'](\w+)["\']', text))
    assert len(model_tables) >= 37, f"模型表数量异常：{len(model_tables)}（应 ≥37）"
    missing = sorted(model_tables - defined)
    assert not missing, f"模型表未在 04-数据库设计说明书 登记：{missing}"


def test_new_fr_numbers_do_not_conflict():
    """FR 编号连续性守卫。

    历史：FR-421 为 D160 引入的最大编号；FR-422~425（v1.5 追加）、FR-426（D170 分诊）、
    FR-427（D173 技能体系升级）依次登记。**上限随新需求递增，但不允许跳号**——
    跳号意味着某次发版漏登记需求编号。
    """
    prd = (ROOT / "docs/01-需求与设计/开发需求文档-运维监控平台-v1.0.md").read_text(encoding="utf-8")
    used = {int(x) for x in re.findall(r"FR-(\d+)", prd)}
    assert max(used) == 428, f"PRD 最大 FR 应为 428，实际 {max(used)}"
    for n in (422, 423, 424, 425, 426, 427):
        assert n in used, f"PRD 缺 FR-{n}"
    assert "v1.5 追加需求（2026-10-21，FR-422 ~ FR-425）" in prd
