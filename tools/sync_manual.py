#!/usr/bin/env python3
"""把用户手册（Markdown + 截图 + PDF）同步进前端静态目录，供平台内置「使用手册」页读取。

为什么用脚本同步而不是直接提交副本：
  · **单一来源**：手册真身始终是 `docs/04-交付与运营/用户手册.md`（面向客户的版本；
    内部版是 `用户手册.内部.md`，**不随包分发**）。若把副本提交进仓库，等于同一份内容
    存两处，改一处忘一处必然漂移。
  · 镜像构建时由 `deploy/Dockerfile.platform` 直接 COPY（同一份源文件），
    本脚本只解决**本地开发**时 `static/` 下没有手册的问题。

产物（均被 .gitignore 忽略，不提交）：
    static/manual/manual.md      手册正文
    static/manual/manual.pdf     手册 PDF（D231：页面下载入口已删，PDF 仅作公众号发放物料）
    static/manual/images/*.png   截图（正文按相对路径引用）

用法：
    python tools/sync_manual.py          # 同步
    python tools/sync_manual.py --check  # 只校验是否已同步且与源一致（回归锁用）
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "04-交付与运营"
SRC_PDF = DOCS / "用户手册.pdf"
SRC_IMAGES = DOCS / "images"

# 随平台内置的**客户版**文档（内部版 用户手册.内部.md 不在此列，绝不随包分发）
DOCS_TO_SHIP = [
    ("用户手册.md", "manual.md"),
    ("快速开始.md", "quickstart.md"),
]

DEST = ROOT / "static" / "manual"
DEST_MD = DEST / "manual.md"          # 兼容旧名（--check 用）
DEST_PDF = DEST / "manual.pdf"
DEST_IMAGES = DEST / "images"


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def _sync() -> int:
    missing = [src for src, _ in DOCS_TO_SHIP if not (DOCS / src).exists()]
    if missing:
        print(f"[manual] 源文档不存在：{missing}", file=sys.stderr)
        return 1
    DEST.mkdir(parents=True, exist_ok=True)
    total = 0
    # 版本号占位符要在这里落地：正文里写的是 `{{VERSION}}`（单一来源），
    # 生成器（gen_manual_docs.py）给 Word/PDF 替换，**文档中心读的是这里拷贝出去的 md**，
    # 原文照拷会把 `{{VERSION}}` 直接显示给用户（实测：手册正文里出现"当前版本（{{VERSION}}）"）。
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "dev"
    for src, dst in DOCS_TO_SHIP:
        text = (DOCS / src).read_text(encoding="utf-8").replace("{{VERSION}}", "v" + ver)
        (DEST / dst).write_text(text, encoding="utf-8")
        total += (DEST / dst).stat().st_size
    n_img = 0
    if SRC_IMAGES.is_dir():
        DEST_IMAGES.mkdir(parents=True, exist_ok=True)
        for img in SRC_IMAGES.iterdir():
            if img.is_file():
                shutil.copy2(img, DEST_IMAGES / img.name)
                n_img += 1
    pdf_note = ""
    if SRC_PDF.exists():
        shutil.copy2(SRC_PDF, DEST_PDF)
        pdf_note = f"、PDF {SRC_PDF.stat().st_size // 1024}KB"
    # 演示视频（带字幕版）：文档中心要能播 -> 拷到 static/manual/videos/demo.mp4
    video_note = ""
    demo_vid = DOCS / "videos" / "demo-latest" / "demo-v1.2.1_配音_字幕.mp4"
    if not demo_vid.exists():
        demo_vid = DOCS / "videos" / "demo-latest" / "demo-v1.2.1_配音_minimax.mp4"   # 兜底：无字幕版
    if demo_vid.exists():
        DEST_VIDEOS = DEST / "videos"
        DEST_VIDEOS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(demo_vid, DEST_VIDEOS / "demo.mp4")
        en_vid = DOCS / "videos" / "demo-latest" / "demo-v1.2.1_en.mp4"
        if en_vid.exists():
            shutil.copy2(en_vid, DEST_VIDEOS / "demo-en.mp4")
        poster_src = DOCS / "videos" / "demo-latest" / "demo-poster.jpg"
        if poster_src.exists():
            shutil.copy2(poster_src, DEST_VIDEOS / "demo-poster.jpg")
        # 缓存击穿（D94）：手册正文里视频 URL 固定为 demo.mp4，浏览器对 206 分片的启发式
        # 缓存会让客户继续看旧成片（实测：换新版后仍播旧视频，"字幕和音频对不上"）。
        # 在**同步产物**的 md 里给 URL 追加内容指纹（源 md 保持干净，不影响生成器）。
        vhash = _md5(DEST_VIDEOS / "demo.mp4")[:8]
        manual_dst = DEST / "manual.md"
        manual_dst.write_text(
            manual_dst.read_text(encoding="utf-8")
            .replace("videos/demo.mp4", f"videos/demo.mp4?v={vhash}")
            .replace("videos/demo-poster.jpg", f"videos/demo-poster.jpg?v={vhash}"),
            encoding="utf-8")
        video_note = f"、演示视频 {demo_vid.stat().st_size // 1024}KB"
    print(f"[manual] 已同步：{len(DOCS_TO_SHIP)} 篇正文 {total // 1024}KB、截图 {n_img} 张{pdf_note}{video_note} → {DEST}")
    return 0


def _check() -> int:
    """校验：产物存在且与源逐字节一致（供回归锁调用，防止手册改了忘同步）。"""
    problems: list[str] = []
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "dev"
    for src, dst in DOCS_TO_SHIP:
        s_path, d_path = DOCS / src, DEST / dst
        if not s_path.exists():
            problems.append(f"源文档缺失：{s_path}")
        elif not d_path.exists():
            problems.append(f"未同步：{dst}（运行 python tools/sync_manual.py）")
        else:
            expect = s_path.read_text(encoding="utf-8").replace("{{VERSION}}", "v" + ver)
            # 同步产物里的视频 URL 带内容指纹（缓存击穿），比对时按同样的规则还原
            vfile = DEST / "videos" / "demo.mp4"
            if vfile.exists():
                vh = _md5(vfile)[:8]
                expect = (expect.replace("videos/demo.mp4", f"videos/demo.mp4?v={vh}")
                          .replace("videos/demo-poster.jpg", f"videos/demo-poster.jpg?v={vh}"))
            if expect != d_path.read_text(encoding="utf-8"):
                problems.append(f"{src} 与同步产物不一致（改过但未重新同步）")
    if SRC_PDF.exists() and not DEST_PDF.exists():
        problems.append("PDF 未同步")
    if SRC_IMAGES.is_dir():
        missing = [p.name for p in SRC_IMAGES.iterdir() if p.is_file() and not (DEST_IMAGES / p.name).exists()]
        if missing:
            problems.append(f"截图缺 {len(missing)} 张（如 {missing[0]}）")
    demo_vid = DOCS / "videos" / "demo-latest" / "demo-v1.2.1_配音_字幕.mp4"
    if demo_vid.exists() and not (DEST / "videos" / "demo.mp4").exists():
        problems.append("演示视频未同步到 static/manual/videos/demo.mp4")
    if problems:
        for x in problems:
            print(f"[manual] ✗ {x}", file=sys.stderr)
        return 1
    print("[manual] ✓ 文档资源已同步且与源一致")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验一致性，不写文件")
    args = ap.parse_args()
    return _check() if args.check else _sync()


if __name__ == "__main__":
    sys.exit(main())
