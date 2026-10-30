#!/usr/bin/env python3
"""为《测试报告》截取当前状态的证据截图（真实浏览器 + 真实数据）。

与《手册》截图不同：这里拍的是"测试证据"——例如 D20 修复后 AI 诊断**成功**的报告页。
选择器沿用 videos/make_tour.py 已实测探明的（抽屉必须点 .drawer-mask 关闭，Esc 无效）。
"""
import asyncio, sys
from pathlib import Path
from playwright.async_api import async_playwright, Page

BASE = "http://127.0.0.1:8848"
OUT = Path(__file__).resolve().parent.parent / "docs/04-交付与运营/images"
OUT.mkdir(parents=True, exist_ok=True)


async def shot(page: Page, name: str):
    await page.screenshot(path=str(OUT / name))
    print(f"  📷 {name}")


async def nav(page: Page, path: str, hold: float = 2.5):
    await page.evaluate(f'location.hash = "{path}"')
    await page.wait_for_timeout(int(hold * 1000))


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = await b.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = await ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text[:160]) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)[:160]))

        await page.goto(f"{BASE}/#/login", wait_until="networkidle")
        await page.wait_for_timeout(1200)
        await page.locator("input.login-input").nth(0).fill("admin")
        await page.locator("input[type=password]").first.fill("admin123")
        await page.locator("button:has-text('登录')").first.click()
        await page.wait_for_timeout(3500)
        await shot(page, "t-report-01-dashboard.png")

        # ---- 问题看板 → 打开事件 → AI 诊断报告（D20 修复后的成功产物）----
        await nav(page, "/incidents", hold=3.0)
        await shot(page, "t-report-02-incidents.png")
        cards = page.locator(".inc-card")
        n = await cards.count()
        print(f"  事件卡片数：{n}")
        if n:
            # 选有诊断报告的那张（标题含"探活失败"的是 1000033）
            target = 0
            for i in range(n):
                t = (await cards.nth(i).inner_text()) or ""
                if "探活失败" in t:
                    target = i
                    break
            await cards.nth(target).click()
            await page.wait_for_timeout(3500)
            await shot(page, "t-report-03-diagnose-report.png")
            print("  已打开事件详情（含诊断报告）")

        # ---- 巡检执行（长任务，真实耗时）----
        await nav(page, "/inspections", hold=2.5)
        await shot(page, "t-report-04-inspections.png")

        # ---- 操作审计（越权拒绝留痕，D8-05 证据）----
        await nav(page, "/audit", hold=2.5)
        await shot(page, "t-report-05-audit.png")

        # ---- 系统设置（改密卡片，D18-1 证据）----
        await nav(page, "/settings", hold=2.5)
        await shot(page, "t-report-06-settings.png")

        print(f"  控制台错误：{len(errors)}")
        for e in errors[:5]:
            print("   ", e)
        await b.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
