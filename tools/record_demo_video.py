# -*- coding: utf-8 -*-
"""AI 运维监控平台 · 全流程录屏脚本（Playwright 驱动真实浏览器 + 录制 webm）。
用法：python3 /tmp/record_demo.py [base_url]
输出：video 到 ./docs/04-交付与运营/videos/demo-latest/ ；segment 时间轴到同目录 timeline.json
"""
import os, sys, json, time, datetime
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://203.0.113.11:8848"
OUT  = os.path.expanduser("~/aiops-platform/docs/04-交付与运营/videos/demo-latest")
os.makedirs(OUT, exist_ok=True)
EXE  = "os.path.expanduser("~/Library/Caches/ms-playwright")/chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"

timeline = []          # [ {seg, at_ms, hold_s} ]
def mark(seg, hold):
    timeline.append({"seg": seg, "at_ms": int(time.time()*1000), "hold": hold, "ts": datetime.datetime.now().strftime("%H:%M:%S")})
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] >>> {seg}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=EXE, args=["--start-maximized"])
    ctx = browser.new_context(viewport={"width":1500,"height":920}, device_scale_factor=1,
                              record_video_dir=OUT, record_video_size={"width":1500,"height":920})
    page = ctx.new_page()
    page.set_default_timeout(20000)

    def nav(hash_, hold=4.5):
        page.goto(BASE + hash_, wait_until="domcontentloaded")
        page.wait_for_timeout(900)
        try: page.wait_for_load_state("networkidle", timeout=5000)
        except Exception: pass
        page.wait_for_timeout(int(hold*1000))
        mark(hash_, hold)

    # 1) 登录
    mark("login·登录", 1)
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(1800)
    try:
        page.fill("input[type='text']", "admin")
        page.fill('input[name="password"], input[type="password"]', "admin123")
        # 点登录按钮
        btn = page.locator("button:has-text(\'登录\'):not(:has-text(\'退出\'))").first
        btn.click()
        page.wait_for_timeout(3500)
    except Exception as e:
        print("login fill err", e)
    mark("login·已登录进入", 2)

    # 2) 大盘总览
    nav("#/dashboard", 6)
    # 3) 主机管理
    nav("#/hosts", 5)
    # 4) 告警中心
    nav("#/alerts", 6)
    # 5) 问题看板
    nav("#/incidents", 5)
    # 6) 打开一个「已解决」事件详情（它已有 AI 诊断报告，不必触发新诊断 → 不烧 token、不阻塞）
    mark("incident·事件详情打开", 1)
    try:
        card = page.locator(".inc-card", has_text="已解决").first
        card.wait_for(state="visible", timeout=9000)
        page.wait_for_timeout(800)
        card.click()                                  # 点已解决卡片 → 打开详情（含现成报告）
        page.wait_for_timeout(3200)
    except Exception as e:
        print("incident detail err", e)
    mark("incident·诊断报告", 6)
    # 6.5) 关键：看完关抽屉！否则它是 fixed 遮罩，会挡住后面所有页面（上一版录屏的坑，D91）
    try:
        page.locator(".detail-head button").first.click()     # 点 ✕
        page.wait_for_selector(".detail-panel", state="detached", timeout=6000)
    except Exception:
        try:
            page.locator(".drawer-mask").first.click()        # 兜底：点遮罩关闭
            page.wait_for_timeout(800)
        except Exception:
            pass
    # 7) 服务监控
    nav("#/services", 5)
    # 8) 处置与变更
    nav("#/runbooks", 5)
    # 9) 案例库
    nav("#/cases", 5)
    # 10) AI 助手：真实问"过去24小时恢复了哪些告警、最慢是哪条"
    nav("#/copilot", 3)
    mark("copilot·提问", 1)
    try:
        page.fill(".chat-input", "过去24小时恢复了哪些告警？恢复到最慢的是哪条，大概多久？")
        page.locator('.chat-input-row button:has-text("发送")').first.click()
        # 等回答完成（最迟 45s）
        deadline = time.time() + 45
        while time.time() < deadline:
            page.wait_for_timeout(2500)
            try:
                last = page.locator(".chat-list .msg").last
                txt = last.inner_text()
                if not (("思考中" in txt) or ("回答中" in txt) or ("查阅了" in txt and len(txt) < 40)):
                    break
            except Exception:
                pass
    except Exception as e:
        print("chat err", e)
    page.wait_for_timeout(5000)
    mark("copilot·已回答（含上下文条）", 8)
    # 11) 文档中心（manual 页）+ 版本信息（about 页）。
    # 注意：文档中心路由是 #/manual（不是 #/docs——无效 hash 会回退到大盘，上一版录屏的坑，D91）；版本信息是 #/about。
    nav("#/manual", 6)
    nav("#/about", 6)
    mark("docs·版本信息", 5)

    video = page.video
    video_path = video.path() if video else None
    page.wait_for_timeout(800)
    # 收尾
    mark("end·结束", 0)
    ctx.close(); browser.close()
    with open(os.path.join(OUT,"timeline.json"),"w",encoding="utf-8") as f:
        json.dump({"base":BASE,"timeline":timeline,"video":video_path}, f, ensure_ascii=False, indent=2)
    print("\n== DONE ==  video:", video_path)
