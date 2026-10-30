#!/usr/bin/env python3
"""用 MiniMax T2A 语音合成演示视频旁白并合成进成片。

论证（D92，用户给的 MiniMax key 可调音频接口）：
  · 端点 https://api.minimaxi.com/v1/t2a_v2，Authorization: Bearer <sk-…>，无需 GroupId；
  · 返回 data.audio 是 **hex 字符串**（不是 base64！）——bytes.fromhex 即得 MP3（32kHz 单声道）；
  · 本机 python ssl 校验失败，故用 subprocess 调 curl 发请求。

用法：
  python tools/gen_voiceover_minimax.py [--voice male-qn-jingying] [--speed 1.0] [视频目录]
产物（视频目录）：
  - 配音旁白_minimax.mp3
  - demo-v1.2.1-配音_minimax.mp4
依赖：curl；key 从环境变量 MINIMAX_TTS_KEY 读取（不要写进代码）。
"""
import argparse
import base64
import json
import os
import pathlib
import subprocess
import sys

ENDPOINT = "https://api.minimaxi.com/v1/t2a_v2"
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
GAP = 0.4


def _dur(path: str) -> float:
    r = subprocess.run(["ffmpeg", "-i", path], capture_output=True, text=True).stderr
    import re
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r)
    if not m:
        return 0
    h, mi, s = map(float, m.groups())
    return h * 3600 + mi * 60 + s


def t2a(text: str, token: str, voice: str, speed: float, out: str) -> float:
    body = json.dumps({
        "model": "speech-02-turbo", "text": text, "stream": False,
        "voice_setting": {"voice_id": voice, "speed": speed},
        "audio_setting": {"sample_rate": 32000, "format": "mp3"},
    })
    raw = subprocess.run(
        ["curl", "-s", "-m", "90", "-X", "POST", ENDPOINT,
         "-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json",
         "-d", body], capture_output=True, text=True).stdout
    d = json.loads(raw)
    b = (d.get("data") or {}).get("audio")
    if not b:
        raise RuntimeError(f"T2A 失败: {d.get('base_resp')}")
    pathlib.Path(out).write_bytes(bytes.fromhex(b))   # data.audio 是 hex，不是 base64
    return _dur(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="male-qn-jingying")
    ap.add_argument("--speed", type=float, default=1.15)
    ap.add_argument("--dir", default="docs/04-交付与运营/videos/demo-latest")
    ap.add_argument("--src", default="demo-v1.2.1.mp4")
    a = ap.parse_args()
    token = os.environ.get("MINIMAX_TTS_KEY", "").strip()
    if not token:
        sys.exit("请设置环境变量 MINIMAX_TTS_KEY 再运行")
    vdir = pathlib.Path(a.dir)
    os.makedirs("/tmp/tts_mm", exist_ok=True)
    clips = []
    for i, txt in enumerate(NARRATION):
        out = f"/tmp/tts_mm/l{i:02d}.mp3"
        d = t2a(txt, token, a.voice, a.speed, out)
        clips.append(out)
        print(f"#{i:02d} {d:4.1f}s  {txt[:28]}")
    # 拼接（段间静音）
    gab = f"/tmp/tts_mm/sil.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"anullsrc=r=32000:cl=mono", "-t", str(GAP), "-c:a", "libmp3lame",
                    gab], check=True)
    with open("/tmp/tts_mm/list.txt", "w") as f:
        for c in clips:
            f.write(f"file '{os.path.realpath(gab)}'\nfile '{os.path.realpath(c)}'\n")
        f.write(f"file '{os.path.realpath(gab)}'\n")
    nar = "/tmp/tts_mm/narration.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", "/tmp/tts_mm/list.txt", "-c:a", "libmp3lame", "-b:a", "160k", nar], check=True)
    ns = _dur(nar)
    print(f"√ 旁白总时长 {ns:.1f}s")
    # 单条 mp3 + 合入成片
    out_mp3 = vdir / f"配音旁白_minimax.mp3"
    out_v = vdir / f"{pathlib.Path(a.src).stem}_配音_minimax.mp4"
    subprocess.run(["cp", nar, str(out_mp3)])
    vd = _dur(str(vdir / a.src))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-i", str(vdir / a.src), "-i", nar,
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                    "-af", "apad,loudnorm=I=-16:TP=-1.5:LRA=11", "-t", f"{vd:.2f}", str(out_v)], check=True)
    print("√", out_mp3)
    print("√", out_v)


if __name__ == "__main__":
    main()