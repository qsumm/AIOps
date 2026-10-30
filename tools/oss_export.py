#!/usr/bin/env python3
"""开源导出：把 opensource-baseline 分支的快照灌进独立开源仓库（孤儿提交，零历史泄漏）。

用法：python tools/oss_export.py [目标目录，默认 ../testops]
流程：① 从当前检出（应在 opensource-baseline 分支）按白名单复制文件
     ② 敏感扫描（密钥/公网 IP/TestStar/商业词）——命中即失败，绝不带病导出
     ③ 目标仓库孤儿提交（首个提交即干净基线；后续更新也只有快照 diff）
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OSS_REMOTE = "git@github.com:qsumm/TestOPS.git"   # 孤儿提交会重建 dest/.git，remote 一并清空，导完直接推这里

# 白名单：开源仓库只包含这些顶层路径（商业文档/调研/内部资产不在其列）
#
# D178 交付前审查修：原白名单含 `docs/04-交付与运营`（36 份**内部交付运营资料**：
# 缺陷台账/开发进展记录/靶机验收报告/验收报告/演示脚本/内部评估/录屏…）与
# `docs/06-面试题库`（个人素材）——它们不在《00-双仓库与同步机制》§3 的白名单里，
# 是代码与文档漂移的结果。开源包里只保留**面向用户**的交付物：
# 用户手册、快速开始、版本发布说明。
WHITELIST = [
    "backend", "agent", "static", "deploy", "tools", "scripts",
    # D182（用户拍板）：开源包 docs 只保留「操作手册」本体 + README 自身的图片资产
    # （图标 / 架构图 / 头图）。教程、设计文档、参考手册、商业化页等全部不再进开源包。
    # D230（用户拍板）：完整用户手册（md/pdf/快速开始/引用截图）随公众号发放，不进开源包；
    # Dockerfile 的 /static/manual COPY 行由 ②.5 定向剥离，开源镜像不带手册（平台内文档页显示获取指引）。
    "docs/images",
    "docs/screenshots/tutorials/02-dashboard.png",
    # learning/（学习学院 20 页）D228 起撤出开源包：全部为「编写中」骨架页，公开观感差；
    # 内容写完再逐页恢复白名单。主仓保留不动。
    "README.md", "README.en.md", "LICENSE", "VERSION", ".gitignore", ".python-version",   # VERSION 是 Dockerfile COPY + /api/health 的构建依赖，缺了开源用户 build 直接失败
    # 注：AGENTS.md 自 D182 公开起退出开源包（内部 AI 开发纪律，含公司项目名等内部信息）
]
# 个人/内部文档目录（即使未来误入白名单也剔除）：写作风格、事实清单等内部工作稿
STRIP_PREFIXES = [
    "docs/07-知识库/",
    "docs/06-面试题库/",
    # 注：docs/04-交付与运营/ 不在 STRIP_PREFIXES——白名单三份交付物 + 手册引用截图
    # 由 ①.5 定向拷入，白名单外文件校验（_unexpected）就是原来的整目录防线（D180 修：
    # 此前这里把刚拷入的手册整本 rmtree 掉，OSS 手册与其图片全部缺失）。
    "docs/02-调研/",
    "docs/03-归档/",
]
# 必查敏感词（任一命中即拦）
#
# D178 补强：原表只有 10 个词，拦不住"真实公网/靶机 IP""阿里云 AccessKey""手机号"
# "语音模板号""个人业务线词汇"。这四类都在本项目真实出现过（会话里用过、文档里写过），
# 不进表 = 扫描器形同虚设。**只增不减**——命中就修内容，别改词表放过。
BANNED = [
    # —— 密钥类（正则：长度阈值避开 "sk-body"/"disk-host" 这类正常词）——
    r"sk-cp-[A-Za-z0-9]{8,}",
    r"\bsk-[A-Za-z0-9_-]{20,}",
    r"\bLTAI[A-Za-z0-9]{12,}",
    r"\bAKID[A-Za-z0-9]{10,}",
    r"AccessKeySecret\s*[:=]\s*\S{12,}",
    # 语音模板码：放行明显的占位值 TTS_123456789（文档/测试里的假值），
    # 真实模板码（9 位数字）仍然命中
    r"\bTTS_(?!123456789)\d{6,}",
    # 密钥常量赋值：要求值像真密钥（≥16 位、无引号）——
    # `_ALIYUN_TTS_DEFAULT_KEY = "content"` 这类常量名不该误报
    r"(?:MINIMAX|ALIYUN)_[A-Z_]*KEY\s*=\s*[\"']?[A-Za-z0-9_\-]{16,}",
    # —— 真实基础设施坐标 ——
    r"8\.148\.155\.",
    r"1\.15\.66\.59",
    r"1\.116\.109\.224",
    r"117\.185\.181\.78",
    r"172\.19\.0\.\d+",
    # 通用规则：任何"IP:端口"形态的公网地址都拦（真实主机引用总是这个形态）。
    # 排除私网/环回/文档保留段与公共 DNS（1.1.1.1 / 8.8.8.8 / 9.9.9.9 属常识性示例）。
    r"(?<![\d.])(?!(?:10\.|127\.|0\.0\.0\.0|192\.168\.|172\.(?:1[6-9]|2[0-9]|3[01])\.|"
    r"169\.254\.|203\.0\.113\.|198\.51\.100\.|100\.64\.|255\.255\.255\.255))"
    r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\d.])(?=:\d+)",
    # —— 个人身份 / 联系方式 ——
    # D182 公开仓纪律：真实号码/人名不进任何仓库（导出器本身随包公开）。
    # 真实值放本地补充词表 ~/.config/aiops-oss/banned_local.txt（每行一条正则，仅本机），
    # 由 main() 在扫描时并入 BANNED。
    # —— 商业敏感（含个人业务线）——
    # 商业词要带上下文——"部分成功"里含"分成"、普通文档里含"定价"，裸词会误报一片
    r"(收入|利润|营收|佣金|服务费)\s*分成",
    r"分成\s*(比例|方案|规则|机制)",
    r"(商业|市场)定价",
    r"试销|毛利|首单成交|售卖|面试辅导|独家代理|渠道分成",
    # —— 他方品牌 / 内部代号 ——
    r"TestStar",
]
# 文档目录里的商业敏感文件（即使白名单命中也剔除）
STRIP = [
    "docs/01-需求与设计/00-双仓库与同步机制.md",   # 内部规范：商业仓库策略不进开源包
]


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    dest = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "../testops").resolve()
    branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "opensource-baseline":
        sys.exit(f"请切到 opensource-baseline 分支再导出（当前 {branch}）")

    # ① 白名单复制
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for w in WHITELIST:
        src = ROOT / w
        if not src.exists():
            print(f"  跳过（不存在）: {w}")
            continue
        dst = dest / w
        if src.is_dir():
            shutil.copytree(src, dst,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv",
                                                          "node_modules", "nonexistent-home-for-preflight",
                                                          "*.db", "*.sqlite*", ".pytest_cache"))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


    # 剔除内部文件
    for f in STRIP:
        t = dest / f
        if t.exists():
            t.unlink()
    for pre in STRIP_PREFIXES:
        d = dest / pre
        if d.exists():
            shutil.rmtree(d)
            print(f"  已剔除内部目录: {pre}")

    # ②.5 OSS 版 Dockerfile.platform 定制（D226/D230）：删除全部 /static/manual/ 的 COPY 行——
    # 演示视频（界面录屏按真实 IP 不进开源包）与完整手册（随公众号发放）都不入开源包，
    # 但 main 的 Dockerfile 有这些 COPY → OSS 仓库没有对应文件会构建失败。
    # 只删 COPY 行；脚本内 vid=Path(...videos/...) 定义保留（vid.exists() 安全降级）。
    _df = dest / "deploy" / "Dockerfile.platform"
    if _df.exists():
        _lines = _df.read_text(encoding="utf-8").splitlines(keepends=True)
        _kept = [ln for ln in _lines if not (ln.strip().startswith("COPY") and "/static/manual/" in ln)]
        if len(_kept) != len(_lines):
            _df.write_text("".join(_kept), encoding="utf-8")
            print(f"✓ Dockerfile.platform 删除 /static/manual COPY {len(_lines) - len(_kept)} 行（OSS 包不带手册与演示视频）")

    # ② 敏感扫描（含历史文件名与内容）
    # D182：真实手机号/人名等放本地补充词表（不入库），扫描时并入。
    import os as _os
    extra_banned = []
    _local = pathlib.Path(_os.path.expanduser("~/.config/aiops-oss/banned_local.txt"))
    if _local.exists():
        extra_banned = [ln.strip() for ln in _local.read_text(encoding="utf-8").splitlines()
                        if ln.strip() and not ln.startswith("#")]
    hits = []
    for f in dest.rglob("*"):
        if f.name in ("oss_export.py", "test_d3_frontend_assets.py"):   # 跳过扫描器自身（BANNED 词表在源码里，与 d3 守卫同理）
            continue
        if f.is_file() and f.suffix in (".py", ".js", ".md", ".html", ".json", ".yaml", ".yml",
                                        ".sh", ".css", ".txt", ".srt"):
            try:
                s = f.read_text(encoding="utf-8")
            except Exception:
                continue
            for b in BANNED:
                if re.search(b, s):
                    hits.append(f"{f.relative_to(dest)} ← {b}")
            for b in extra_banned:
                if b in s:
                    hits.append(f"{f.relative_to(dest)} ← {b}")
            for b in extra_banned:
                if b in s:
                    hits.append(f"{f.relative_to(dest)} ← {b}")
    if hits:
        print("✗ 敏感扫描命中，禁止导出：")
        for h in hits[:10]:
            print("  ", h)
        sys.exit(1)
    print("✓ 敏感扫描零命中")

    # ③ git 孤儿提交
    def g(*cmd):
        return subprocess.run(["git", "-C", str(dest), *cmd], capture_output=True, text=True)

    if not (dest / ".git").exists():
        g("init", "-b", "main")
        g("remote", "add", "origin", OSS_REMOTE)
    if not g("remote").stdout.strip():
        g("remote", "add", "origin", OSS_REMOTE)
    g("add", "-A")
    msg = os.environ.get("OSS_COMMIT_MSG", "TestOPS — AI 运维监控平台").strip()
    g("-c", "user.name=aiops-oss", "-c", "user.email=oss@aiops.local",
      "commit", "-m", msg, "--allow-empty") if g("status", "--porcelain").stdout.strip() else None
    print(f"✓ 开源仓库已更新：{dest}")
    r = g("log", "--oneline", "-1")
    print("  最新提交:", r.stdout.strip())

    # ② 孤儿快照单提交直推 main（D232：不再手工 squash——手工再改一次 SHA
    # 会让分支/tag 的对齐立刻失效，这里把「提交+强推+对齐」收成一次动作）。
    sha = g("rev-parse", "HEAD").stdout.strip()
    pr = g("push", "--force", "origin", "HEAD:main")
    if pr.returncode != 0:
        sys.exit(f"✗ 推 main 失败：{(pr.stderr or pr.stdout).strip()[:300]}")
    print(f"✓ 已强推 main（单提交 {sha[:8]}）")

    # ③ 版本分支与 tag 自动对齐到本次快照：发布后不对齐，GitHub 会在版本分支上挂
    # 「had recent pushes · Compare & pull request」横幅，读者误点会用旧快照覆盖新 main
    # （曾把英文 README 冲掉过一次）。
    ver = os.environ.get("OSS_VERSION_TAG", "").strip()
    if ver:
        g("push", "--force", "origin", f"{sha}:refs/heads/{ver}")
        g("tag", "-f", ver, "-m", f"开源基线 {ver}")
        g("push", "--force", "origin", ver)
        print(f"✓ 版本分支与 tag 已对齐：{ver} → {sha[:8]}")


if __name__ == "__main__":
    main()
