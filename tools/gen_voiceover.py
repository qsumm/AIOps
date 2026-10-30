#!/usr/bin/env python3
"""为演示视频生成中文旁白并混入成片（macOS 自带 TTS「婷婷」，无需任何云密钥）。

用法：python tools/gen_voiceover.py [视频目录]
产物（写进视频目录）：
  - 配音旁白.mp3       单条连续旁白
  - demo-v1.2.1-配音.mp4   把旁白作为音轨合成进 demo-v1.2.1.mp4

说明：
  · 用 macOS `say`（ron：婷婷，简体中文），离线、零成本、可复现；
  · 旁白为「时间轴顺序」的一条连续音轨（段间 0.35s 静音），语速 220 字/分以适配 ~119s 成片；
  · 想要更自然的神经网络音色（如 MiniMax / 火山 / 微软 TTS），把下面 NARRATION 交给对应 TTS
    再替换 /tmp/tts/narration.wav 后重跑合成段即可（需配置相应 TTS 密钥）。
"""
import os
import pathlib
import subprocess
import sys

# 简体中文旁白（时间轴顺序，精简版，适配 Tingting 语速）
NARRATION = [
    "这是一套 AI 运维监控平台。我们通过一条真实故障，从告警到事件、诊断到处置、再到沉淀经验，完整走一遍。",
    "登录后，先看整体态势。",
    "大盘显示，当前纳管八台主机，其中四台失联。指标、告警、事件都被收敛成一张总览。",
    "主机列表里四台离线，像一次集体失联。Agent 心跳停止，平台如实标红。",
    "告警中心里是当前还触发的告警，按主机和规则分组，严重度一目了然。",
    "告警会被收敛成事件。问题看板现在有二十个事件，十七个待处理。",
    "点开真实事件，能看到成员告警，以及 AI 诊断的错误上下文：日志证据和时间线。",
    "AI 报告给出根因、证据，和验证过的修复建议。结论都带引用。",
    "服务监控按实例看健康度：连接池、进程、接口，是正常、异常还是不可用。",
    "验证过的修复建议，可一键生成变更单，带风险分级和回滚方案。",
    "事件关闭自动沉淀成案例卡，还能语义检索——把每次故障变成下次的参考。",
    "AI 助手能直接查平台数据，并带上下文管理，记得住更早的对话。",
    "我们问它：过去二十四小时恢复了哪些告警？最慢是哪条？",
    "它调用了告警历史工具：最慢的告警是内存异常检测，126 秒恢复；并如实指出，这更像是演练数据而非真实故障。",
    "文档中心内置使用手册，随平台分发，还能在线搜索。",
    "版本信息确认当前是 1.2.1，构建号能定位到具体提交。",
    "从告警收敛成事件、AI 定位根因、生成处置、沉淀案例——这就是平台闭环的价值。",
]

VOICE = "Tingting"      # macOS 简体中文音色「婷婷」
RATE = 220              # 语速（字/分）
GAP = 0.35              # 段间静音（秒）
SR = 22050


def _dur(f: str) -> float:
    r = subprocess.run(["ffmpeg", "-i", f], capture_output=True, text=True).stderr
    import re
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r)
    if not m:
        return 0
    h, mi, s = map(float, m.groups())
    return h * 3600 + mi * 60 + s


def main(vdir: str):
    vdir = pathlib.Path(vdir)
    os.makedirs("/tmp/tts", exist_ok=True)
    clips = []
    for i, txt in enumerate(NARRATION):
        a, w = f"/tmp/tts/l{i:02d}.aiff", f"/tmp/tts/l{i:02d}.wav"
        subprocess.run(["say", "-v", VOICE, "-r", str(RATE), "-o", a, txt], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", a, "-ar", str(SR), "-ac", "1", w], check=True)
        clips.append(w)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"anullsrc=r={SR}:cl=mono", "-t", str(GAP), "-c:a", "pcm_s16le",
                    "/tmp/tts/sil.wav"], check=True)
    with open("/tmp/tts/list.txt", "w") as f:
        for c in clips:
            f.write(f"file '/tmp/tts/sil.wav'\nfile '{c}'\n")
        f.write("file '/tmp/tts/sil.wav'\n")
    narration = "/tmp/tts/narration.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", "/tmp/tts/list.txt", "-c:a", "pcm_s16le", narration], check=True)
    nar_s = _dur(narration)
    print(f"√ 旁白 {nar_s:.1f}s")

    # 单条旁白 mp3
    mp3 = vdir / "配音旁白.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", narration, "-c:a", "libmp3lame", "-q:a", "4", str(mp3)], check=True)
    print("√", mp3)

    # 混入成片
    src = vdir / "demo-v1.2.1.mp4"
    outv = vdir / "demo-v1.2.1-配音.mp4"
    vdur = _dur(str(src))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-i", str(src), "-i", narration,
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                    "-af", "apad,loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-t", f"{vdur:.2f}", str(outv)], check=True)
    print("√", outv)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/04-交付与运营/videos/demo-latest")