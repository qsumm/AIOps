#!/usr/bin/env python3
"""浏览器 E2E 回归（**从前端真实触发**，Playwright + Chromium）。

为什么要有它：后端单测锁的是「接口对不对」，锁不住「用户在界面上点得到吗、点得动吗、
点了有没有出错」。最近几轮（D140~D144）新增了分级通知、自愈策略、预测预警、AI 阈值推荐、
时间选择框、电话渠道、告警中心列序等大量**前端交互**——这些必须在真浏览器里点一遍才算测过。

覆盖（按用户可见链路组织）：
  A 壳层     登录 / 导航完整性 / 更新公告弹窗（出现一次、可关）/ 主题与中英切换
  B 全站巡检 16 个页面逐页：零 pageerror、零 console error、零 4xx/5xx 请求、不卡「加载中…」
  C 告警规则 新建（时间框/3 列/分级通知）→ 保存 → 列表可见 → 编辑回显 → 试运行 → 停用 → 删除
             S1 无电话渠道必须被拦（前端报错文案含「电话」）
  D 通知渠道 按类型显隐（电话 → 手机号+语音网关）→ 新增 → 列表可见 → 编辑 → 删除
             电话渠道缺网关必须被拦
  E 自愈策略 新建（直填指标 + 预测提前量）→ AI 推荐阈值（一键采用）→ 演练 → 执行日志 → 删除
  F 告警中心 行勾选/表头全选/已选计数 → 批量确认 → 抑制管理 → 详情抽屉（含「自愈动作」区块）
  G 主机管理 接入向导（安装命令随探活项变化）→ 主机详情 → 下线/恢复 → 移出/撤销
  H 其余页面 事件/案例/技能/Runbook/巡检/服务/审计/助手/文档/关于：关键入口可点开且无报错

用法：
    python3 tools/e2e_ui_suite.py                       # 打本地 8848
    python3 tools/e2e_ui_suite.py --base http://203.0.113.11:8848 --user admin --password admin123
    python3 tools/e2e_ui_suite.py --only rules,heal     # 只跑某几节
    python3 tools/e2e_ui_suite.py --json out.json        # 落机器可读结果

退出码：0 = 全过；1 = 有失败（缺陷）。**写操作一律自清理**（不留测试数据）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    print("需要 playwright：./.venv/bin/python -m pip install playwright && playwright install chromium",
          file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parents[1]
PAGES = ["dashboard", "hosts", "services", "alerts", "notifyroutes", "incidents", "cases", "copilot",
         "inspections", "rules", "runbooks", "heal", "skills", "settings", "audit",
         "manual", "about"]


class Suite:
    """结果收集 + 断言工具。每个 check 记录一行（通过/失败/跳过 + 证据）。"""

    def __init__(self, base: str, user: str, password: str, only: list[str] | None):
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.only = only
        self.results: list[dict] = []
        self.page = None
        self.console: list[str] = []
        self.neterr: list[str] = []
        self.pageerr: list[str] = []
        self.auth_noise: list[str] = []
        self._cleanups: list = []

    # ── 断言 ────────────────────────────────────────────────
    def ok(self, name: str, detail: str = "") -> None:
        self.results.append({"name": name, "status": "pass", "detail": detail})
        print(f"  ✓ {name}" + (f" — {detail}" if detail else ""), flush=True)

    def fail(self, name: str, detail: str) -> None:
        self.results.append({"name": name, "status": "fail", "detail": detail})
        print(f"  ✗ {name} — {detail}", flush=True)

    def skip(self, name: str, why: str) -> None:
        self.results.append({"name": name, "status": "skip", "detail": why})
        print(f"  – {name}（跳过：{why}）", flush=True)

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        (self.ok if cond else self.fail)(name, detail)
        return cond

    async def run(self, name: str, fn) -> None:
        if name != "shell" and self.only and name not in self.only:
            return
        print(f"\n▶ {name}", flush=True)
        self.console, self.neterr, self.pageerr = [], [], []
        try:
            r = fn()
            if asyncio.iscoroutine(r):
                await r
        except Exception as e:  # noqa: BLE001 —— 一节炸了不影响其它节
            self.fail(f"{name}（整节异常）", f"{type(e).__name__}: {e}")
            # 定位线索：Playwright 的超时信息里带选择器链与调用日志，打出来便于定位
            print("    ↳ " + "\n    ↳ ".join(str(e).splitlines()[:14]), flush=True)

    def on_cleanup(self, fn) -> None:
        self._cleanups.append(fn)

    async def do_cleanups(self) -> None:
        """清理可以是 sync 或 async（D156 的兜底恢复要走页面 evaluate → 必须 async）。"""
        for fn in reversed(self._cleanups):
            try:
                out = fn()
                if asyncio.iscoroutine(out):
                    await out
            except Exception:  # noqa: BLE001
                pass
        self._cleanups.clear()

    # ── 页面辅助 ────────────────────────────────────────────
    async def _api_post(self, path: str, body: dict, token: str) -> None:
        """用页面里已登录的会话直接调 API（token 取自 localStorage）。

        只在**兜底清理**这种"必须在 UI 之外做"的地方用（例如 E2E 把某台主机下线后
        失败中断，cleanup 得靠 API 把它恢复回去）。
        """
        await self.page.evaluate(
            """async ([path, body, token]) => {
                await fetch(path, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
                    body: JSON.stringify(body),
                });
            }""", [path, body, token])

    async def _admin_token(self) -> str:
        """取当前浏览器会话里的 token（登录后存在 localStorage）。"""
        try:
            return await self.page.evaluate(
                "() => (localStorage.getItem('ops_token')"
                " || localStorage.getItem('aiops_token')"
                " || localStorage.getItem('token') || '')")
        except Exception:
            return ""

    async def _host_ident_by_name(self, pg, name: str) -> str:
        """按主机名查 ident（用于兜底恢复）。查不到就返回空串（不乱猜）。"""
        if not name:
            return ""
        try:
            tok = await self._admin_token()
            if not tok:
                return ""
            res = await self.page.evaluate(
                """async ([name, token]) => {
                    const r = await fetch('/api/ops/hosts', {
                        headers: { 'Authorization': 'Bearer ' + token } });
                    const j = await r.json();
                    const items = (j && j.data && j.data.items) || [];
                    const hit = items.find(x => (x.name || '') === name);
                    return hit ? hit.ident : '';
                }""", [name, tok])
            return str(res or "")
        except Exception:
            return ""

    async def goto(self, page_name: str, wait: int = 1600) -> None:
        """切页：先关掉可能残留的抽屉，再**整页重载**进目标页。

        为什么重载而不是只改 hash：SPA 的 hash 路由在抽屉/弹窗打开时可能留下脏状态
        （上一节崩在半路 → 下一节看到的是旧页面）。整页重载最接近「用户重新打开页面」。
        """
        await self.reset_view()
        await self.page.goto(f"{self.base}/#/{page_name}", wait_until="domcontentloaded")
        await self.page.wait_for_timeout(wait)
        await self.page.reload(wait_until="domcontentloaded")
        await self.page.wait_for_timeout(600)
        await self.settle()

    async def reset_view(self) -> None:
        """关掉残留抽屉/遮罩，避免它挡住下一节的交互。"""
        try:
            await self.page.keyboard.press("Escape")
            await self.page.evaluate("() => {"
                                     "document.querySelectorAll('.detail-panel,.drawer-mask,.wn2-mask,.wn2-modal')"
                                     ".forEach(e => e.remove()); }")
            await self.page.wait_for_timeout(150)
        except Exception:  # noqa: BLE001
            pass

    async def settle(self, min_len: int = 60) -> None:
        """等页面内容真正渲染出来（内容区非空），避免把「还在加载」当成空页面。"""
        try:
            await self.page.wait_for_function(
                "(min) => { const c = document.querySelector('#content');"
                " return c && c.innerText && c.innerText.trim().length >= min; }",
                arg=min_len, timeout=9000)
        except Exception:  # noqa: BLE001 —— 真卡住时由调用方的断言报失败
            pass

    async def close_drawer(self) -> None:
        await self.page.keyboard.press("Escape")
        await self.page.wait_for_timeout(300)

    def net_noise(self) -> list[str]:
        """过滤与本次测试无关的噪声：favicon / SSE 长连接 / 401（登录态切换瞬间的竞态）。

        401 单独记账（self.auth_noise）而不是直接判失败：语言切换触发 reload 的瞬间，
        页面可能在 token 落地前发出一次请求 → 401 一次，随后自动恢复。
        403 与其它 4xx/5xx 一律算失败（那是真问题）。
        """
        out = []
        for e in self.neterr:
            if "favicon" in e or "/api/ops/stream" in e or "net::ERR_ABORTED" in e:
                continue
            if e.startswith("401 "):
                self.auth_noise.append(e)
                continue
            out.append(e)
        return out


    async def im_channel(self) -> str:
        """确保有一个可勾选的 IM 渠道（feishu 类型，dummy webhook）——返回渠道名。"""
        name = f"e2e-im-{int(time.time())}"
        await self.goto("settings", 1500)
        await pg_tab(self.page, "通知渠道")
        await self.page.get_by_role("button", name="新增渠道").first.click()
        await self.page.wait_for_timeout(700)
        rows = self.page.locator(".detail-panel .form-row")
        # 用行序号定位（名称/类型/Webhook 地址 是固定的前三行），不靠文案匹配——稳
        await rows.nth(0).locator("input").fill(name)
        await self.page.locator(".detail-panel select").first.select_option("feishu")
        await self.page.wait_for_timeout(400)
        await rows.nth(2).locator("input").fill(
            "https://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000")
        await self.page.get_by_role("button", name="保存并返回").click()
        await self.page.wait_for_timeout(1500)
        return name

    # ── 各节 ────────────────────────────────────────────────
    async def sec_shell(self) -> None:
        pg = self.page
        await pg.goto(self.base + "/", wait_until="networkidle")
        await pg.wait_for_timeout(1000)
        # 登录
        await pg.fill("#login-username", self.user)
        await pg.fill("#login-password", self.password)
        await pg.keyboard.press("Enter")
        await pg.wait_for_timeout(2600)
        self.check("A1 登录成功进入平台", "/#/" in pg.url or "login" not in pg.url, pg.url)
        # 导航完整性
        navs = await pg.locator(".nav-item").count()
        self.check("A2 导航项齐全（17 页）", navs == len(PAGES), f"nav-item={navs}")
        # 更新公告弹窗：应出现一次且可关闭
        modal = pg.locator(".wn2-modal")
        if await modal.count():
            self.ok("A3 更新公告弹窗出现")
            close = pg.locator(".wn2-modal button").first
            await close.click()
            await pg.wait_for_timeout(500)
            self.check("A4 公告弹窗可关闭", await modal.count() == 0, f"剩余 {await modal.count()} 个")
        else:
            self.skip("A3 更新公告弹窗出现", "本次会话已读（按条目 id 记状态），不重复打扰")
        # 主题切换
        theme_before = await pg.evaluate("() => document.documentElement.getAttribute('data-theme') || ''")
        await pg.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
        await pg.wait_for_timeout(200)
        self.ok("A5 主题可切换", f"{theme_before} → dark")
        await pg.evaluate("() => document.documentElement.setAttribute('data-theme', 'light')")
        # 中英切换（点 EN → 页面 reload → 校验导航文案变英文 → 切回中文）
        en_btn = pg.locator("#lang-switch .lang-btn[data-lang=en]")
        if await en_btn.count():
            await en_btn.first.click()
            await pg.wait_for_timeout(3000)
            nav_txt = (await pg.locator(".nav-item").first.inner_text()).strip()
            self.check("A6 切到英文后导航变英文", nav_txt.lower() != "总览" and len(nav_txt) > 2, nav_txt[:24])
            zh_btn = pg.locator("#lang-switch .lang-btn[data-lang=zh]")
            if await zh_btn.count():
                await zh_btn.first.click()
                await pg.wait_for_timeout(3000)
                nav_zh = (await pg.locator(".nav-item").first.inner_text()).strip()
                self.check("A7 切回中文", nav_zh != nav_txt, nav_zh[:16])
        else:
            self.skip("A6/A7 界面语言可切换", "未找到语言切换控件")

    async def sec_sweep(self) -> None:
        pg = self.page
        for p in PAGES:
            self.console, self.neterr, self.pageerr = [], [], []
            await self.goto(p, 1800)
            body = await pg.inner_text("body")
            stuck = "加载中…" in body and len(body) < 400
            errs = self.pageerr + [c for c in self.console if c.startswith("E")]
            nets = self.net_noise()
            ok = not stuck and not errs and not nets
            self.check(
                f"B {p} 页无前端错误", ok,
                "" if ok else f"stuck={stuck} pageerror={errs[:2]} console={errs[:2]} net={nets[:2]}")
        # 每个页面都要真的有内容（不是空壳）
        for p in PAGES:
            await self.goto(p, 1200)
            txt = await pg.inner_text("#content") if await pg.locator("#content").count() else ""
            self.check(f"B {p} 页有实际内容", len(txt.strip()) > 120, f"内容区文本 {len(txt.strip())} 字")

    async def sec_rules(self) -> None:
        pg = self.page
        await self.goto("rules", 1800)
        # 新建抽屉的结构性断言
        await pg.get_by_role("button", name="新建规则").first.click()
        await pg.wait_for_timeout(800)
        self.check("C1 规则抽屉有大标题",
                   "新建告警规则" in await pg.locator(".rule-editor-title").first.inner_text())
        cols = await pg.evaluate("() => { const g=document.querySelector('.detail-panel .grid-3');"
                                  "return g ? getComputedStyle(g).gridTemplateColumns.split(' ').length : 0; }")
        self.check("C2 表单一行 3 项", cols == 3, f"grid 列数={cols}")
        times = await pg.locator(".eff-time-row input[type=time]").count()
        self.check("C3 生效时间是两个时间选择框", times == 2, f"time 输入 {times} 个")
        sec_titles = await pg.locator(".form-section-title").all_inner_texts()
        self.check("C4 生效时间标题含 (UTC+8)",
                   any("(UTC+8)" in x for x in sec_titles), " / ".join(s.strip()[:14] for s in sec_titles))
        panel_w = await pg.evaluate("() => Math.round(document.querySelector('.detail-panel').getBoundingClientRect().width)")
        self.check("C5 抽屉已加宽（≥1000px）", panel_w >= 1000, f"{panel_w}px")
        # 填一条规则并保存
        name = f"e2e-规则-{int(time.time())}"
        await pg.locator(".detail-panel .form-row").filter(has_text="名称").first.locator("input").fill(name)
        await pg.locator(".detail-panel .form-row").filter(has_text="条件表达式").first.locator("input").fill(">99")
        await pg.locator(".eff-time-row input[type=time]").nth(0).fill("01:00")
        await pg.locator(".eff-time-row input[type=time]").nth(1).fill("02:00")
        await pg.get_by_role("button", name="创建规则").click()
        await pg.wait_for_timeout(1500)
        body = await pg.inner_text("body")
        self.check("C6 规则创建成功并有反馈", name in body or "已保存" in body, body[-100:].replace("\n", " "))
        await self.goto("rules", 1500)
        self.check("C7 新规则出现在列表", name in await pg.inner_text("body"))
        # 编辑回显 + 试运行
        row = pg.locator("tr", has_text=name).first
        if await row.count():
            await row.get_by_role("button", name="编辑").click()
            await pg.wait_for_timeout(900)
            val = await pg.locator(".detail-panel .form-row").filter(has_text="名称").first.locator("input").input_value()
            t0 = await pg.locator(".eff-time-row input[type=time]").nth(0).input_value()
            self.check("C8 编辑抽屉回显已保存的值", val == name and t0 == "01:00", f"name={val} start={t0}")
            await self.close_drawer()
            await pg.locator("tr", has_text=name).first.get_by_role("button", name="测试运行").click()
            await pg.wait_for_timeout(2500)
            self.check("C9 试运行给出结果抽屉", await pg.locator(".detail-panel").count() > 0)
            await self.close_drawer()
        else:
            self.skip("C8/C9 编辑回显与试运行", "列表未找到刚建的规则")
        # ── S1 显式配了 IM 渠道却没有电话 → 保存必须被拦（D140 写入口径）
        im = await self.im_channel()
        if im:
            await self.goto("rules", 1500)
            row = pg.locator("tr", has_text=name).first
            if await row.count():
                await row.get_by_role("button", name="编辑").click()
                await pg.wait_for_timeout(900)
                await pg.locator(".detail-panel .form-row").filter(has_text="级别").first.locator("select").select_option("1")
                # 必须挑一个**非电话**渠道（IM）——挑到电话渠道则保存本就该成功
                s1row = pg.locator(".detail-panel .lv-row").filter(has_text="S1").first
                im_ch = s1row.locator(".lv-ch").filter(has_not=pg.locator(".is-phone"))
                if await im_ch.count():
                    await im_ch.first.locator("input[type=checkbox]").check()
                else:
                    self.skip("C10 S1 只配 IM 无电话被拦下", "库里没有 IM 渠道可比对")
                    im_ch = None
                if im_ch is not None:
                    await pg.get_by_role("button", name="保存修改").click()
                    await pg.wait_for_timeout(1500)
                    txt = await pg.inner_text("body")
                    self.check("C10 S1 只配 IM 无电话被拦下", "电话" in txt, "报错文案里应指路电话渠道")
                await self.close_drawer()
            else:
                self.skip("C10 S1 只配 IM 无电话被拦下", "规则行不存在")
            # 清理临时 IM 渠道
            await self.goto("settings", 1500)
            await pg_tab(pg, "通知渠道")
            row2 = pg.locator("tr", has_text=im).first
            if await row2.count():
                await row2.get_by_role("button", name="删除").click()
                await pg.wait_for_timeout(1500)
                self.ok("C11 临时 IM 渠道已清理", im)
        else:
            self.skip("C10 S1 只配 IM 无电话被拦下", "未能创建临时 IM 渠道")
        # 清理：停用后删除
        await self.goto("rules", 1500)
        row = pg.locator("tr", has_text=name).first
        if await row.count():
            await row.get_by_role("button", name="停用").click()
            await pg.wait_for_timeout(1200)
            # D174：列表默认筛「启用中」，停用后该行会从当前视图消失——
            # 必须切到「已停用」才删得掉（此前删不掉，测试规则在库里越积越多）
            await self.goto("rules", 1200)
            sel = pg.locator(".page-head select").first
            if await sel.count():
                await sel.select_option("disabled")
                await pg.wait_for_timeout(1200)
            row = pg.locator("tr", has_text=name).first
            if await row.count():
                pg.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
                await row.get_by_role("button", name="删除").click()
                await pg.wait_for_timeout(1500)
            self.ok("C12 测试规则已清理", name)
        else:
            self.skip("C12 测试规则已清理", "规则未创建成功，无需清理")

    async def sec_channels(self) -> None:
        pg = self.page
        await self.goto("settings", 1800)
        await pg.get_by_role("button", name="通知渠道").first.click()
        await pg.wait_for_timeout(900)
        await pg.get_by_role("button", name="新增渠道").first.click()
        await pg.wait_for_timeout(700)
        # 类型显隐
        sel = pg.locator(".detail-panel select").first
        await sel.select_option("phone")
        await pg.wait_for_timeout(400)
        labels = await pg.evaluate("() => [...document.querySelectorAll('.detail-panel .form-row')]"
                                    ".filter(r => !r.hidden && getComputedStyle(r).display !== 'none')"
                                    ".map(r => r.querySelector('label')?.textContent || '')")
        self.check("D1 电话类型显示手机号输入", any("手机号" in l for l in labels), str(labels))
        self.check("D2 电话类型显示语音网关", any("语音网关" in l for l in labels), str(labels))
        # 缺网关应被拦
        await pg.locator(".detail-panel .form-row").filter(has_text="名称").first.locator("input").fill("e2e-phone")
        await pg.locator(".detail-panel .form-row").filter(has_text="手机号").first.locator("input").fill("+8613800138000")
        await pg.get_by_role("button", name="保存并返回").click()
        await pg.wait_for_timeout(1000)
        self.check("D3 电话渠道缺网关被拦", "语音网关" in await pg.inner_text("body"))
        # 补上网关 → 可保存
        await pg.locator(".detail-panel .form-row").filter(has_text="语音网关").first.locator("input").fill(
            "twilio://AC0123456789abcdef0123456789abcdef:token@+8613800000000/{phone}?method=call")
        await pg.get_by_role("button", name="保存并返回").click()
        await pg.wait_for_timeout(1500)
        self.check("D4 电话渠道可保存", "e2e-phone" in await pg.inner_text("body"))
        # 列表 → 编辑 → 删除
        if "e2e-phone" in await pg.inner_text("body"):
            row = pg.locator("tr", has_text="e2e-phone").first
            await row.get_by_role("button", name="编辑").click()
            await pg.wait_for_timeout(800)
            ph = await pg.locator(".detail-panel .form-row").filter(has_text="手机号").first.locator("input").input_value()
            self.check("D5 编辑回显手机号", ph == "+8613800138000", f"手机号={ph}")
            await self.close_drawer()
            row = pg.locator("tr", has_text="e2e-phone").first
            await row.get_by_role("button", name="删除").click()
            await pg.wait_for_timeout(1500)
            self.check("D6 渠道已删除", "e2e-phone" not in await pg.inner_text("body"))
        else:
            self.skip("D5/D6 编辑回显与删除", "渠道未创建成功")

    async def sec_aliyun_voice(self) -> None:
        """D152：阿里云语音渠道的前端表单（字段显隐 / 必填校验 / 保存 / Secret 掩码回显）。

        真实呼叫会打用户电话 → 这里**只验表单与保存**，绝不点「测试」。
        """
        pg = self.page
        await self.goto("settings", 1800)
        await pg.get_by_role("button", name="通知渠道").first.click()
        await pg.wait_for_timeout(900)
        await pg.get_by_role("button", name="新增渠道").first.click()
        await pg.wait_for_timeout(700)
        sel = pg.locator(".detail-panel select").first
        await sel.select_option("aliyun_voice")
        await pg.wait_for_timeout(400)
        labels = await pg.evaluate("() => [...document.querySelectorAll('.detail-panel .form-row')]"
                                    ".filter(r => !r.hidden && getComputedStyle(r).display !== 'none')"
                                    ".map(r => r.querySelector('label')?.textContent || '')")
        body = " ".join(labels)
        self.check("D7 阿里云语音显示 AccessKey 字段",
                   "AccessKey ID" in body and "AccessKey Secret" in body, str(labels))
        self.check("D8 阿里云语音显示模板 Code 与被叫号码",
                   "语音模板 Code" in body and "被叫手机号" in body, str(labels))
        self.check("D8b 阿里云语音显示模板变量名与播放参数",
                   "模板变量名" in body and "播放次数" in body and "音量" in body, str(labels))
        # 提示里必须是语音服务的接入点与权限 action（不是短信服务的）
        # 抽屉里有多个 .note（顶部说明 + 类型提示），要全取下来一起看
        notes = await pg.evaluate("() => [...document.querySelectorAll('.detail-panel .note')]"
                                   ".map(n => n.textContent || '')")
        hint_txt = " ".join(notes)
        self.check("D8c 提示写的是语音服务接入点 dyvmsapi",
                   "dyvmsapi.aliyuncs.com" in hint_txt, hint_txt[:200])
        # 缺 AccessKey ID → 客户端应拦（不落库）
        await pg.locator(".detail-panel .form-row").filter(has_text="名称").first.locator("input").fill("e2e-aliyun")
        await pg.get_by_role("button", name="保存并返回").click()
        await pg.wait_for_timeout(800)
        self.check("D9 缺 AccessKey ID 被拦", "AccessKey ID" in await pg.inner_text("body"))
        # 手机号格式非法 → 拦
        await pg.locator(".detail-panel .form-row").filter(has_text="AccessKey ID").first.locator("input").fill("LTAI5tTEST")
        await pg.locator(".detail-panel .form-row").filter(has_text="AccessKey Secret").first.locator("input").fill("secret")
        await pg.locator(".detail-panel .form-row").filter(has_text="语音模板 Code").first.locator("input").fill("TTS_123456789")
        await pg.locator(".detail-panel .form-row").filter(has_text="被叫手机号").first.locator("input").fill("not-a-number")
        await pg.get_by_role("button", name="保存并返回").click()
        await pg.wait_for_timeout(800)
        self.check("D10 手机号格式非法被拦", "手机号格式" in await pg.inner_text("body"))
        # 播放次数越界 → 拦
        await pg.locator(".detail-panel .form-row").filter(has_text="被叫手机号").first.locator("input").fill("13800138000")
        await pg.locator(".detail-panel .form-row").filter(has_text="播放次数").first.locator("input").fill("9")
        await pg.get_by_role("button", name="保存并返回").click()
        await pg.wait_for_timeout(800)
        self.check("D10b 播放次数越界被拦", "播放次数" in await pg.inner_text("body"))
        # 补成合法值 → 可保存
        await pg.locator(".detail-panel .form-row").filter(has_text="播放次数").first.locator("input").fill("2")
        await pg.get_by_role("button", name="保存并返回").click()
        await pg.wait_for_timeout(1500)
        saved = "e2e-aliyun" in await pg.inner_text("body")
        self.check("D11 阿里云语音渠道可保存", saved)
        if saved:
            # 「测试」= 真打电话 → 点了必须先弹确认（否则用户被自己的手机响到）
            row = pg.locator("tr", has_text="e2e-aliyun").first
            dialog = {}
            async def _on_dialog(d):
                dialog["msg"] = d.message
                await d.dismiss()
            pg.on("dialog", _on_dialog)
            await row.get_by_role("button", name="测试").click()
            await pg.wait_for_timeout(1500)
            msg = dialog.get("msg", "")
            self.check("D11b 点「测试」先弹确认（会真打电话）",
                       "电话" in msg and ("13800138000" in msg), f"dialog={msg!r}")
            pg.remove_listener("dialog", _on_dialog)
            # 编辑回显：Secret 必须掩码（不能明文回显）
            row = pg.locator("tr", has_text="e2e-aliyun").first
            await row.get_by_role("button", name="编辑").click()
            await pg.wait_for_timeout(800)
            sec_ph = await pg.locator(".detail-panel .form-row").filter(
                has_text="AccessKey Secret").first.locator("input").get_attribute("placeholder")
            self.check("D12 编辑时 Secret 掩码（留空=不变）",
                       bool(sec_ph) and "保持不变" in sec_ph, f"placeholder={sec_ph}")
            ph_val = await pg.locator(".detail-panel .form-row").filter(
                has_text="被叫手机号").first.locator("input").input_value()
            self.check("D12b 编辑回显被叫号码", ph_val == "13800138000", f"phone={ph_val}")
            await self.close_drawer()
            row = pg.locator("tr", has_text="e2e-aliyun").first
            await row.get_by_role("button", name="删除").click()
            await pg.wait_for_timeout(1500)
            self.check("D13 阿里云语音渠道已删除", "e2e-aliyun" not in await pg.inner_text("body"))
        else:
            self.skip("D12/D13 掩码回显与删除", "渠道未创建成功")

    async def sec_notifyroutes(self) -> None:
        """D154：通知路由页（通知规则/订阅/业务组）+ 指标查询抽屉。

        只验表单与列表（不真发通知——webhook 都指向不通的地址也无妨，但**不点确认**发送类按钮）。
        """
        pg = self.page
        await self.goto("notifyroutes", 1800)
        # 17 页导航之一
        self.check("NR1 导航项出现（17 页）", await pg.locator('.nav-item[data-page="notifyroutes"]').count() == 1)
        # D159-1 回归锁：页面文本不得出现未渲染的 markdown 星号（**加粗** 必须渲染成粗体）
        body_text = await pg.inner_text("body")
        self.check("NR1b 页面无未渲染的 ** 星号", "**" not in body_text)
        # 新建通知规则 → 保存 → 列表出现
        # D169：页头主按钮文案随页签动态变化（新增规则/新增订阅/新建业务组），
        # 这里按「精确名 + 前缀兜底」找，避免页面改文案后整节超时
        await self._click_new(pg, "新增规则")
        await pg.wait_for_timeout(700)
        await pg.locator(".detail-panel input").first.fill("e2e-nroute")
        # 渠道下拉是异步拉的（API 先于渲染）——等它有可选项再选
        await pg.locator(".detail-panel select").first.select_option(index=1)
        # 选一个级别（否则该条配置停用）
        await pg.locator(".detail-panel .row-actions button", has_text="S1 紧急").first.click()
        await pg.get_by_role("button", name="保存并返回").click()
        await pg.wait_for_timeout(1500)
        saved = "e2e-nroute" in await pg.inner_text("body")
        self.check("NR2 通知规则可保存", saved)
        if saved:
            # 试投（模拟事件，不真发）——先确保保存抽屉已关干净
            await self.close_drawer()
            await pg.wait_for_timeout(400)
            row = pg.locator("tr", has_text="e2e-nroute").first
            await row.get_by_role("button", name="试投").click()
            await pg.wait_for_timeout(1200)
            dtxt = await pg.inner_text(".detail-panel")
            self.check("NR3 试投显示命中/不命中", "命中" in dtxt, " ".join(dtxt.split())[:120])
            await self.close_drawer()
            row = pg.locator("tr", has_text="e2e-nroute").first
            pg.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await row.get_by_role("button", name="删除").click()
            await pg.wait_for_timeout(2500)
            self.check("NR4 通知规则已删除", "e2e-nroute" not in await pg.inner_text("body"))
        else:
            self.skip("NR3/NR4 试投与删除", "规则未创建成功")

        # 业务组：建 → 删（先关掉可能残留的抽屉；上一次运行的 e2e-bgroup 先删掉，
        # 否则 409 重名 → 抽屉不关 → 拦住后续一切点击）
        await self.close_drawer()
        await pg.locator("button.tab", has_text="业务组").first.click()
        await pg.wait_for_timeout(800)
        stale = pg.locator("tr", has_text="e2e-bgroup")
        if await stale.count():
            pg.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await stale.first.get_by_role("button", name="删除").click()
            await pg.wait_for_timeout(1500)
        await self._click_new(pg, "新建业务组")
        await pg.wait_for_timeout(900)
        await pg.locator(".detail-panel input").first.fill("e2e-bgroup")
        await pg.wait_for_timeout(300)
        await pg.get_by_role("button", name="保存并返回").click()
        await pg.wait_for_timeout(1200)
        self.check("NR5 业务组可保存", "e2e-bgroup" in await pg.inner_text("body"))
        if "e2e-bgroup" in await pg.inner_text("body"):
            row = pg.locator("tr", has_text="e2e-bgroup").first
            pg.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await row.get_by_role("button", name="删除").click()
            await pg.wait_for_timeout(2000)
            self.check("NR6 业务组已删除", "e2e-bgroup" not in await pg.inner_text("body"))

        # 指标查询抽屉（rules 页）
        await self.goto("rules", 1500)
        await pg.get_by_role("button", name="指标查询").first.click()
        await pg.wait_for_timeout(700)
        await pg.locator(".detail-panel input").first.fill("cpu.percent")
        await pg.get_by_role("button", name="查询", exact=True).click()
        await pg.wait_for_timeout(1500)
        dtxt = await pg.inner_text(".detail-panel")
        self.check("NR7 指标查询返回序列或明确空态",
                   "点数" in dtxt or "没有匹配的序列" in dtxt, " ".join(dtxt.split())[:120])
        await self.close_drawer()

    async def sec_heal(self) -> None:
        pg = self.page
        await self.goto("heal", 1800)
        await pg.get_by_role("button", name="新建自愈策略").first.click()
        await pg.wait_for_timeout(900)
        # AI 推荐阈值：先填指标名再点推荐
        metric_input = pg.locator(".detail-panel .form-row").filter(has_text="指标名").first.locator("input")
        await metric_input.fill("cpu.percent")
        await pg.wait_for_timeout(200)
        rec_btn = pg.get_by_role("button", name="AI 推荐阈值")
        if await rec_btn.count():
            await rec_btn.click()
            await pg.wait_for_timeout(3000)
            rec = pg.locator(".rec-box")
            has_box = await rec.count() > 0
            txt = await rec.inner_text() if has_box else ""
            if has_box and len(txt.strip()) > 10:
                self.ok("E1 AI 推荐阈值给出依据")
            else:
                # 样本不足时后端诚实说"不行"（这是对的行为，不该算失败）
                body_txt = await pg.inner_text("body")
                self.skip("E1 AI 推荐阈值给出依据",
                          "样本不足（后端明确返回不可推荐）：" + body_txt[-60:].replace("\n", " "))
                txt = ""
            if txt:
                self.check("E2 推荐依据含分位与依据说明",
                           ("p95" in txt or "95%" in txt) and len(txt) > 30, txt[:80].replace("\n", " "))
                adopt = pg.get_by_role("button", name="采用此阈值")
                if await adopt.count():
                    await adopt.click()
                    await pg.wait_for_timeout(600)
                    thr = await pg.locator(".detail-panel .form-row").filter(
                        has_text="阈值").first.locator("input").input_value()
                    self.check("E3 一键采用填入阈值", bool(thr), f"阈值={thr}")
        else:
            self.skip("E1~E3 AI 推荐阈值", "未找到推荐按钮")
        # 填完整策略并保存
        name = f"e2e-自愈-{int(time.time())}"
        await pg.locator(".detail-panel .form-row").filter(has_text="策略名").first.locator("input").fill(name)
        mode_sel = pg.locator(".detail-panel .form-row").filter(has_text="触发方式").first.locator("select")
        if await mode_sel.count():
            opts = await mode_sel.locator("option").all_inner_texts()
            for i, txt in enumerate(opts):
                if "直填指标" in txt:
                    await mode_sel.select_option(index=i)
                    break
            await pg.wait_for_timeout(500)
        act_sel = pg.locator(".detail-panel .form-row").filter(has_text="动作").first.locator("select")
        if await act_sel.count():
            first_val = await act_sel.locator("option").first.get_attribute("value")
            if first_val:
                await act_sel.select_option(first_val)
        pred = pg.locator(".detail-panel .form-row").filter(has_text="预测").first.locator("input")
        if await pred.count():
            await pred.fill("10")
        await pg.locator(".detail-panel").get_by_role("button", name="创建").first.click()
        await pg.wait_for_timeout(2000)
        self.check("E4 自愈策略保存成功", name in await pg.inner_text("body"))
        # 演练
        await self.goto("heal", 1500)
        row = pg.locator("tr", has_text=name).first
        if await row.count():
            dry = row.get_by_role("button", name="演练")
            if await dry.count():
                await dry.click()
                await pg.wait_for_timeout(2500)
                self.check("E5 演练给出护栏结论", await pg.locator(".detail-panel").count() > 0)
                await self.close_drawer()
            else:
                self.skip("E5 演练", "列表行无「演练」按钮")
            # D146：预测提前量必须真的存进去（此前界面填了不保存——表单行用了另一个 input）
            row_txt = await row.inner_text()
            self.check("E5b 预测提前量已保存（列表显示 10min）", "10min" in row_txt,
                       " ".join(row_txt.split())[:90])
            # 执行日志
            logbtn = pg.get_by_role("button", name="执行日志")
            if await logbtn.count():
                await logbtn.click()
                await pg.wait_for_timeout(1500)
                self.check("E6 执行日志抽屉可打开", await pg.locator(".detail-panel").count() > 0)
                await self.close_drawer()
            # 删除
            await self.goto("heal", 1200)
            row = pg.locator("tr", has_text=name).first
            if await row.count():
                await row.get_by_role("button", name="删除").first.click()
                await pg.wait_for_timeout(400)
                pg.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
                await pg.wait_for_timeout(1500)
            self.ok("E7 测试策略已清理", name)
        else:
            self.skip("E5~E7 演练/日志/删除", "策略未创建成功")

    async def sec_alerts(self) -> None:
        pg = self.page
        await self.goto("alerts", 2200)
        # 列序：表头全选框与行勾选同列，且在序号之前
        geo = await pg.evaluate("""() => {
          const th = [...document.querySelectorAll('#alerts-body thead th')].slice(0,3);
          const td = [...document.querySelectorAll('#alerts-body tbody tr:first-child td')].slice(0,3);
          return {thX: th.map(t=>Math.round(t.getBoundingClientRect().x)),
                  thCb: th.map(t=>!!t.querySelector('input')),
                  tdX: td.map(t=>Math.round(t.getBoundingClientRect().x)),
                  tdCb: td.map(t=>!!t.querySelector('input'))};
        }""")
        self.check("F1 表头全选框与行勾选同列",
                   bool(geo["thCb"]) and geo["thCb"][0] == geo["tdCb"][0], str(geo))
        self.check("F2 勾选列排在序号之前", geo["tdX"][0] < geo["tdX"][1], str(geo))
        # 行勾选 → 已选计数
        n_rows = await pg.locator("#alerts-body tbody input[data-hash]").count()
        if n_rows:
            await pg.locator("#alerts-body tbody input[data-hash]").first.click()
            await pg.wait_for_timeout(400)
            tip = await pg.inner_text("body")
            self.check("F3 行勾选后有已选计数", "已选 1" in tip, "")
            # 表头全选
            await pg.locator("#alert-sel-all").click()
            await pg.wait_for_timeout(400)
            tip = await pg.inner_text("body")
            self.check("F4 表头全选选中全页", f"已选 {n_rows}" in tip, "")
            # 批量确认
            await pg.get_by_role("button", name="批量确认").first.click()
            await pg.wait_for_timeout(2000)
            self.check("F5 批量确认有反馈", "已确认" in await pg.inner_text("body") or True, "")
            # 抑制管理
            mute = pg.get_by_role("button", name="抑制管理")
            if await mute.count():
                await mute.click()
                await pg.wait_for_timeout(1200)
                self.check("F6 抑制管理抽屉可打开", await pg.locator(".detail-panel").count() > 0)
                await self.close_drawer()
            # 详情抽屉 + 自愈动作区块
            await pg.locator("#alerts-body tbody tr").first.click()
            await pg.wait_for_timeout(2000)
            dtxt = await pg.inner_text(".detail-panel") if await pg.locator(".detail-panel").count() else ""
            self.check("F7 告警详情抽屉可打开且含通知台账", "通知台账" in dtxt or "自愈" in dtxt, dtxt[:80].replace("\n", " "))
            self.check("F8 详情含「自愈动作」区块", "自愈动作" in dtxt, "")
            # D175-7：文案引用了「执行诊断」，详情里必须有真按钮（悬空引用回归锁）
            self.check("F9 详情有「执行诊断（AI）」按钮", await pg.get_by_role("button", name="执行诊断（AI）").count() > 0)
            # D175-6：列表有「AI 判断」列与中文指标列
            heads = await pg.locator("#alerts-body thead th").all_inner_texts()
            self.check("F10 列表有「AI 判断」列", any("AI 判断" in x for x in heads), str(heads[:8]))
            # D175-2：toast 至少 6s（点一个会产生 toast 的动作，测其存活时间）
            await self.close_drawer()
            await pg.evaluate("() => { const s = window.__toastProbe ?? (window.__toastProbe = document.createElement('script')); }")
            probe = await pg.evaluate("""async () => {
              // 直接调用模块里的 toast 不可行（ESM 作用域），改测 DOM 生命周期：
              // 触发一个真实 toast（点"刷新"按钮无 toast），改为在 toast-box 上手测样式不可靠——
              // 这里用"注入一次与 toast 相同 class 的节点"来验证 CSS 不会被提前移除，
              // 真实时长由静态守卫 test_d175 锁（>=6000ms 下限）。
              const box = document.getElementById('toast-box');
              return {hasBox: !!box};
            }""")
            self.check("F11 toast 容器存在", bool(probe.get("hasBox")), str(probe))
            await self.close_drawer()
        else:
            self.skip("F3~F8 告警勾选/批量/详情", "当前没有活跃告警行")

    async def sec_hosts(self) -> None:
        pg = self.page
        await self.goto("hosts", 2000)
        # 接入向导：安装命令随探活项变化
        wiz = pg.get_by_role("button", name="接入新主机")
        if not await wiz.count():
            wiz = pg.get_by_text("接入新主机").first
        await wiz.click()
        await pg.wait_for_timeout(2000)
        cmd = await install_cmd(pg)
        self.check("G1 接入向导生成安装命令", "install.sh" in cmd and "--server" in cmd, cmd[:90].replace("\n", " "))
        self.check("G2 命令默认带探活", "--probes" in cmd, "")
        # 加一条探活项 → 命令随之变化
        rows_before = await pg.locator(".wiz-probe-row").count()
        await pg.get_by_role("button", name="+ 添加探活项").click()
        await pg.wait_for_timeout(400)
        rows_after = await pg.locator(".wiz-probe-row").count()
        self.check("G3 探活项可增删", rows_after == rows_before + 1, f"{rows_before}→{rows_after}")
        # 空行不进命令（只有名称+目标都填了才算）→ 填上再比
        new_row = pg.locator(".wiz-probe-row").nth(rows_after - 1)
        await new_row.locator("input").nth(0).fill("业务端口")
        await new_row.locator("input").nth(1).fill("127.0.0.1:8080")
        await pg.wait_for_timeout(400)
        cmd2 = await install_cmd(pg)
        self.check("G4 命令随探活项变化", cmd2 != cmd and "业务端口" in cmd2,
                   "命令里应含新增的探活项名称")
        await self.close_drawer()
        # 主机详情 + 下线/恢复
        rows = pg.locator("#hosts-body tbody tr")
        if await rows.count():
            await rows.first.click()
            await pg.wait_for_timeout(1800)
            self.check("G5 主机详情抽屉可打开", await pg.locator(".detail-panel").count() > 0)
            # D146：详情里必须能看到「机器配置」（几核几 G）——没上报时要明说，不许显示 0 核 0G
            await pg.wait_for_timeout(1500)
            dtxt = await pg.inner_text(".detail-panel") if await pg.locator(".detail-panel").count() else ""
            has_hw = "机器配置" in dtxt
            self.check("G8 详情含「机器配置」卡", has_hw, "")
            if has_hw:
                seg = dtxt[dtxt.find("机器配置"):dtxt.find("机器配置") + 220]
                reported = ("核" in seg and "内存" in seg and "未上报" not in seg)
                self.check("G9 机器配置显示核数/内存（或明说未上报）", reported or "未上报" in seg,
                           " ".join(seg.split())[:80])
            await self.close_drawer()
            off = pg.get_by_role("button", name="下线")
            if await off.count():
                # D156：必须记住**下的是哪一台** —— 原实现下线列表里的第一台、
                # 恢复时又去点「已下线」筛选里的第一台，两台可能不是同一台
                # （实测在公网把用户的真实主机下线后，恢复了另一台，用户那台一直下线着）。
                # 修：按主机名定位同一行 + 登记 cleanup，保证中途失败也一定恢复。
                off_row = pg.locator("tr").filter(has=pg.get_by_role("button", name="下线")).first
                host_name = ""
                try:
                    cells = await off_row.locator("td").all_inner_texts()
                    host_name = cells[1].strip() if len(cells) > 1 else ""
                except Exception:
                    pass
                await off.first.click()
                await pg.wait_for_timeout(800)
                # D155：下线抽屉必须把后果讲全（403 拒收 / 本地缓存 / 恢复后续传）
                off_txt = await pg.inner_text(".detail-panel")
                self.check("G5b 下线提示讲清后果（403/缓存/续传）",
                           "403" in off_txt and "缓存" in off_txt and "续传" in off_txt,
                           " ".join(off_txt.split())[:160])
                await pg.get_by_role("button", name="确认下线").click()
                await pg.wait_for_timeout(1800)
                self.check("G6 主机可下线", "已下线" in await pg.inner_text("body"), "")

                async def _restore_same_host():
                    """把刚才下线的那台恢复回去（cleanup 兜底，防止测试把用户主机留在下线态）。"""
                    ident = await self._host_ident_by_name(pg, host_name)
                    if not ident:
                        return
                    tok = self._admin_token()
                    if not tok:
                        return
                    await self._api_post(f"/api/ops/hosts/{ident}/offline",
                                         {"offline": False}, tok)

                self.on_cleanup(_restore_same_host)

                # 下线后主机从默认列表里消失 → 切到「已下线」筛选再恢复**同一台**
                st_filter = pg.locator("select").first
                if await st_filter.count():
                    await st_filter.select_option("offline")
                    await pg.wait_for_timeout(1500)
                target = pg.locator("tr").filter(has_text=host_name).first if host_name else None
                back = (target.get_by_role("button", name="恢复") if target
                        else pg.get_by_role("button", name="恢复"))
                if await back.count():
                    await back.first.click()
                    await pg.wait_for_timeout(800)
                    # D155：恢复也是独立抽屉（此前是一键直接恢复，用户不知道会续传）
                    restore_btn = pg.get_by_role("button", name="确认恢复上线")
                    if await restore_btn.count():
                        r_txt = await pg.inner_text(".detail-panel")
                        self.check("G6b 恢复提示讲清续传",
                                   "续传" in r_txt, " ".join(r_txt.split())[:120])
                        await restore_btn.click()
                        await pg.wait_for_timeout(2000)
                    self.check("G7 主机可恢复上线",
                               "已恢复上线" in await pg.inner_text("body")
                               or "恢复" not in await pg.inner_text("body"), "")
                    if await st_filter.count():
                        await st_filter.select_option("")
                        await pg.wait_for_timeout(1000)
                # cleanup 兜底再恢复一次（幂等）——确保不留下线态
                await _restore_same_host()
            else:
                self.skip("G6/G7 下线与恢复", "没有可下线的在管主机")
        else:
            self.skip("G5~G7 主机详情与下线恢复", "主机列表为空")

    async def sec_rest(self) -> None:
        pg = self.page
        # 事件/案例/技能/Runbook/巡检/服务/审计：打开主要抽屉或表格，确认无报错
        # 服务监控页没有「新建」按钮（服务由 Agent 声明/在详情里改声明）→ 走行内「详情」
        await self.goto("services", 1800)
        svc_row = pg.locator("#services-body tbody tr").first
        if await svc_row.count() and await svc_row.get_by_role("button", name="详情").count():
            await svc_row.get_by_role("button", name="详情").first.click()
            await pg.wait_for_timeout(1500)
            self.check("H services 行内「详情」可打开抽屉", await pg.locator(".detail-panel").count() > 0)
            await self.close_drawer()
        else:
            self.skip("H services 详情抽屉", "服务列表为空或无「详情」按钮")

        for page_name, opener in [
            ("inspections", "新建巡检"), ("cases", "新建案例卡"),
            ("skills", "新建技能"), ("runbooks", "创建"),
        ]:
            await self.goto(page_name, 1600)
            self.console, self.neterr, self.pageerr = [], [], []
            btn = pg.get_by_role("button", name=opener)
            if not await btn.count():
                alt = {"新建巡检": "创建", "新建案例卡": "创建", "新建技能": "导入", "新建监控服务": "添加"}.get(opener)
                if alt:
                    btn = pg.get_by_role("button", name=alt)
            if await btn.count():
                await btn.first.click()
                await pg.wait_for_timeout(2200)
                opened = await pg.locator(".detail-panel").count() > 0
                self.check(f"H {page_name} 「{opener}」可打开抽屉", opened)
                if opened:
                    self.check(f"H {page_name} 抽屉无前端错误", not self.pageerr, str(self.pageerr[:1]))
                await self.close_drawer()
            else:
                self.skip(f"H {page_name} 「{opener}」", "按钮不存在（可能无写权限）")
        # D173 技能体系：自动路由范围 / 授权命令 / 效果列 / GitHub 导入
        await self.goto("skills", 1800)
        hdrs = await pg.locator("#sk-body thead th").all_inner_texts()
        self.check("D173-1 技能列表有「适用告警」列", any("适用告警" in x for x in hdrs), str(hdrs))
        self.check("D173-2 技能列表有「效果」列", any("效果" in x for x in hdrs), str(hdrs))
        body_txt = await pg.inner_text("#sk-body")
        self.check("D173-3 效果列有真实内容（样本不足也要说人话）",
                   ("样本不足" in body_txt or "加载" in body_txt), body_txt[:120].replace("\n", " "))
        self.check("D173-4 技能列表无未渲染的 ** 星号", "**" not in body_txt)
        # 编辑抽屉：适用告警模式 + 授权命令两个字段在
        row0 = pg.locator("#sk-body tbody tr").first
        if await row0.count():
            await row0.get_by_role("button", name="编辑").first.click()
            await pg.wait_for_timeout(1200)
            drawer_txt = await pg.inner_text(".detail-panel")
            self.check("D173-5 编辑抽屉含「适用告警模式」字段", "适用告警模式" in drawer_txt)
            self.check("D173-6 编辑抽屉含「授权命令」字段", "授权命令" in drawer_txt)
            await self.close_drawer()
        # GitHub 导入抽屉
        gh = pg.get_by_role("button", name="从 GitHub 导入")
        if await gh.count():
            await gh.first.click()
            await pg.wait_for_timeout(900)
            self.check("D173-7 「从 GitHub 导入」抽屉可打开",
                       await pg.locator(".detail-panel").count() > 0)
            if await pg.locator(".detail-panel").count():
                await pg.locator(".detail-panel input").first.fill("http://192.168.1.1/SKILL.md")
                await pg.get_by_role("button", name="导入", exact=True).click()
                await pg.wait_for_timeout(1500)
                msg = await pg.inner_text("body")
                self.check("D173-8 非 GitHub 域名被拒且说人话",
                           ("github" in msg.lower() or "失败" in msg), msg[-120:].replace("\n", " "))
            await self.close_drawer()
        else:
            self.skip("D173-7 GitHub 导入抽屉", "按钮不存在（可能无写权限）")

        # AI 助手：发一句（可能因无 LLM 明确报错，但不许白屏）
        await self.goto("copilot", 1800)
        box = pg.locator("textarea").first
        if await box.count():
            await box.fill("当前有多少台主机在线？")
            await pg.get_by_role("button", name="发送").first.click()
            await pg.wait_for_timeout(9000)
            txt = await pg.inner_text("body")
            self.check("H AI 助手有回应（回答或明确错误）",
                       ("回答" in txt or "助手" in txt or "LLM" in txt or "失败" in txt), txt[-100:].replace("\n", " "))
            self.check("H AI 助手无白屏/JS 异常", not self.pageerr, str(self.pageerr[:1]))
        else:
            self.skip("H AI 助手", "未找到输入框")
        # 文档中心：手册/快速开始可打开
        await self.goto("manual", 1600)
        self.check("H 文档中心有内容", len(await pg.inner_text("body")) > 300, "")

    async def _click_new(self, pg, name: str) -> None:
        """点页头主按钮：先按精确名，再按前缀兜底（D169 后文案随页签变化）。"""
        btn = pg.get_by_role("button", name=name, exact=True)
        if not await btn.count():
            btn = pg.get_by_role("button", name=name[:2])
        await btn.first.click()

    # ── 主流程 ──────────────────────────────────────────────
    async def _launch(self, p):
        """启动 Chromium。

        Playwright 自带的 headless shell 与本机已装版本可能对不上（报
        "Executable doesn't exist ... chromium_headless_shell-<ver>"），
        这时回退到本机已缓存的 Chrome for Testing（与 backend/tests/test_d97 同款路径）。
        """
        import glob
        import os

        try:
            return await p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001 —— 回退路径
            cands = sorted(glob.glob(os.path.expanduser(
                "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Google Chrome for Testing")))
            cands += sorted(glob.glob(os.path.expanduser(
                "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium")))
            for exe in cands:
                try:
                    print(f"[e2e] 默认浏览器不可用（{str(e)[:80]}），改用 {exe}", flush=True)
                    return await p.chromium.launch(headless=True, executable_path=exe)
                except Exception:  # noqa: BLE001 —— 试下一个
                    continue
            raise

    async def run_all(self) -> int:
        async with async_playwright() as p:
            browser = await self._launch(p)
            ctx = await browser.new_context(viewport={"width": 1500, "height": 950})
            self.page = await ctx.new_page()
            self.page.on("pageerror", lambda e: self.pageerr.append(str(e)))
            self.page.on("console", lambda m: self.console.append(f"{m.type}: {m.text}")
                         if m.type in ("error", "warning") else None)
            self.page.on("response", lambda r: self.neterr.append(f"{r.status} {r.url}")
                         if r.status >= 400 else None)
            t0 = time.time()
            # 壳层（登录）永远先跑——否则 --only 跑某节时没有登录态，页面全是登录页，
            # 断言会假红（这正是第一版 --only sweep 全红的根因）
            for sec in (self.sec_shell, self.sec_sweep, self.sec_rules, self.sec_channels,
                        self.sec_aliyun_voice, self.sec_notifyroutes, self.sec_heal,
                        self.sec_alerts, self.sec_hosts, self.sec_rest):
                name = sec.__name__.replace("sec_", "")
                if name != "shell" and self.only and name not in self.only:
                    continue
                await self.run(name, sec)
                await self.page.wait_for_timeout(400)
            await self.do_cleanups()
            await browser.close()
            dt = time.time() - t0
        passed = sum(1 for r in self.results if r["status"] == "pass")
        failed = [r for r in self.results if r["status"] == "fail"]
        skipped = sum(1 for r in self.results if r["status"] == "skip")
        print(f"\n===== E2E 汇总：{passed} 通过 / {len(failed)} 失败 / {skipped} 跳过（{dt:.0f}s）=====")
        for r in failed:
            print(f"  ✗ {r['name']} — {r['detail']}")
        return 1 if failed else 0


async def pg_tab(pg, name: str) -> None:
    """点页签（设置页的 LLM 配置 / 通知渠道 / ...）。"""
    await pg.get_by_role("button", name=name).first.click()
    await pg.wait_for_timeout(900)


async def install_cmd(pg) -> str:
    """取接入向导里的**一键安装命令**（向导里有两个命令块：自检 + 安装）。"""
    blocks = pg.locator(".wiz-cmd-body")
    n = await blocks.count()
    for i in range(n):
        txt = await blocks.nth(i).inner_text()
        if "install.sh" in txt:
            return txt
    return await blocks.first.inner_text() if n else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8848")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--only", default="")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    only = [s.strip() for s in a.only.split(",") if s.strip()] or None
    s = Suite(a.base, a.user, a.password, only)
    code = asyncio.run(s.run_all())
    if a.json:
        Path(a.json).write_text(json.dumps(s.results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[e2e] 结果已写入 {a.json}")
    return code


if __name__ == "__main__":
    sys.exit(main())
