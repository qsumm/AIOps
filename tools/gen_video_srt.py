#!/usr/bin/env python3
"""从录屏的时间轴生成字幕 SRT（demo 视频的可复用步骤）。

录屏脚本 tools/record_demo_video.py 会写出 <输出目录>/timeline.json（每段墙钟 + hold）；
本脚把墙钟换算成视频时间偏移，并按 SEGMENTS 里的显示名/旁白生成 `字幕.srt`。

用法：python tools/gen_video_srt.py [videos/demo-latest]
"""
import datetime
import json
import pathlib
import sys

# 段落说明（显示名 + 旁白）。若改动了录屏脚本的段，把这里的键对齐即可。
SEGMENTS = {
    "login·登录": ["开场 · 登录", "这是一套 AI 运维监控平台。下面我们完整走一遍：从告警到事件、从诊断到处置、最后沉淀为平台经验。"],
    "login·已登录进入": ["进入平台", "登录后，我们先看整体态势。"],
    "#/dashboard": ["总览大盘", "大盘显示：当前纳管 8 台主机，其中 4 台失联。平台把指标、告警、事件收敛成一张总览。"],
    "#/hosts": ["主机管理", "主机列表里 4 台处于离线，像一次集体失联。Agent 心跳停止，平台如实标红。"],
    "#/alerts": ["告警中心", "告警中心列出现仍触发的告警，按主机和规则分组，严重度一目了然。"],
    "#/incidents": ["问题看板 · 事件", "告警会被收敛成事件。问题看板现在有 20 个事件，17 个待处理。"],
    "incident·事件详情打开": ["事件详情 + 错误上下文", "点开真实事件，看到收敛的成员告警，以及 AI 诊断要用的错误上下文：日志证据和时间线。"],
    "incident·诊断报告": ["AI 诊断报告", "AI 报告给出根因、证据和验证过的修复建议。结论都带引用。"],
    "#/services": ["服务监控", "服务监控按实例看健康度：连接池、进程、接口，正常、异常还是不可用。"],
    "#/runbooks": ["处置与变更", "验证过的修复建议可一键生成变更单，带风险分级与回滚方案。"],
    "#/cases": ["案例库", "事件关闭自动沉淀成案例卡，还能语义检索——把每次故障变成下次的参考。"],
    "#/copilot": ["AI 助手", "AI 助手能直接查数据，并用上上下文管理，记得住更早的对话。"],
    "copilot·提问": ["AI 助手 · 问答", "我们问它：过去 24 小时恢复了哪些告警，最慢的是哪条？"],
    "copilot·已回答（含上下文条）": ["AI 助手 · 回答", "它调用了告警历史工具，告诉我们最慢的告警是内存异常检测、126 秒恢复，并给出可能原因。"],
    "#/docs": ["文档中心", "文档中心内置使用手册，可以在线搜索。"],
    "docs·版本信息": ["版本信息", "版本信息确认当前是 v1.2.1，构建号可追溯到具体一次提交。"],
    "end·结束": ["收尾", "从告警到事件、诊断、处置、沉淀经验——这就是平台做成一整条闭环的地方。"],
}


def _best():
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def main(dir_: str):
    D = pathlib.Path(dir_)
    tl = json.loads((D / "timeline.json").read_text(encoding="utf-8"))["timeline"]
    start = datetime.datetime.strptime(tl[0]["ts"], "%H:%M:%S")

    def off(t: str) -> float:
        return max(0, (datetime.datetime.strptime(t, "%H:%M:%S") - start).total_seconds())

    def ts2(x: float) -> str:
        h, m = int(x // 3600), int(x % 3600 // 60)
        s = int(x % 60)
        ms = int((x % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    cues = []
    for i, t in enumerate(tl):
        s0 = off(t["ts"])
        e0 = off(tl[i + 1]["ts"]) if i + 1 < len(tl) else s0 + 8
        if e0 <= s0:
            e0 = s0 + 8
        lab, txt = SEGMENTS.get(t["seg"], (t["seg"], ""))
        cues.append((s0, e0, lab, txt))

    lines = []
    for i, (s0, e0, lab, txt) in enumerate(cues, 1):
        lines += [str(i), f"{ts2(s0)} --> {ts2(e0)}", lab]
        if txt:
            lines.append(txt)
        lines.append("")
    out = D / "字幕.srt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"√ 字幕 {len(cues)} 条 → {out.resolve()}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/04-交付与运营/videos/demo-latest")