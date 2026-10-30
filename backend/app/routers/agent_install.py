"""Agent 安装件分发（FR-B2）。

为什么要有这个路由：上手文档让用户执行
    curl -fsSL http://<平台>/install.sh | bash -s -- --server ... --token ...
但 `/install.sh` 之前**返回 404**（静态目录里没有它，镜像里也没带 agent 源码）——
一键安装这条路径实际走不通（手册期实测发现）。这里把 Agent 安装件从源码目录直接分发：
- 开发环境：默认取仓库 `agent/`（可用 AGENT_SRC_DIR 覆盖）
- 容器：镜像把 agent/ 复制到 /agent-src 并设 AGENT_SRC_DIR

同时提供 `/agent_main.py`：`curl | bash` 场景下脚本拿不到本地同级文件，必须能从平台下载。
"""
import hashlib
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

router = APIRouter(tags=["agent-install"])

# 默认：仓库根/agent（app/routers/ → parents[3] 是仓库根）
SRC_DIR = Path(os.getenv("AGENT_SRC_DIR") or (Path(__file__).resolve().parents[3] / "agent"))


def _serve(name: str, media_type: str) -> FileResponse:
    f = SRC_DIR / name
    if not f.is_file():
        raise HTTPException(404, f"AGENT_ARTIFACT_MISSING：未找到 {name}（AGENT_SRC_DIR={SRC_DIR}）")
    return FileResponse(f, media_type=media_type, filename=name)


@router.get("/install.sh", include_in_schema=False)
async def install_script() -> FileResponse:
    return _serve("install.sh", "text/x-shellscript")


@router.get("/agent_main.py", include_in_schema=False)
async def agent_main() -> FileResponse:
    return _serve("agent_main.py", "text/x-python")


def agent_code_sha256() -> str:
    """当前 serving 的 agent_main.py 文件指纹（D121：平台版本漂移判定 = 上报 hash 对比它）。"""
    f = SRC_DIR / "agent_main.py"
    if not f.is_file():
        return ""
    return hashlib.sha256(f.read_bytes()).hexdigest()


@router.get("/agent_main.py.sha256", include_in_schema=False)
async def agent_main_sha256() -> PlainTextResponse:
    """P1-12：安装脚本下载后校验 Agent 文件完整性（防截断/篡改）。

    返回裸 hash（不含文件名），与 install.sh 的 `echo "$HASH  $file" | sha256sum -c -` 配合。
    """
    digest = agent_code_sha256()
    if not digest:
        raise HTTPException(404, f"AGENT_ARTIFACT_MISSING：未找到 agent_main.py（AGENT_SRC_DIR={SRC_DIR}）")
    return PlainTextResponse(digest + "\n", media_type="text/plain")


@router.get("/agent_version", include_in_schema=False)
async def agent_version() -> PlainTextResponse:
    """D144：当前分发 Agent 的版本号（裸文本，如 `0.3.4`）。

    install.sh 用它做「本机已是最新版」的判定——重跑安装命令升级时，
    版本一致就不再覆盖文件，明确打印"无需升级"，而不是假装升级了一遍。
    旧平台没有该端点时脚本回退为从下载内容里解析，不阻断。
    """
    f = SRC_DIR / "agent_main.py"
    if not f.is_file():
        raise HTTPException(404, f"AGENT_ARTIFACT_MISSING：未找到 agent_main.py（AGENT_SRC_DIR={SRC_DIR}）")
    import re
    m = re.search(r'^AGENT_VERSION\s*=\s*"([^"]+)"', f.read_text(encoding="utf-8", errors="ignore"), re.M)
    return PlainTextResponse((m.group(1) if m else "") + "\n", media_type="text/plain")


# 环境自检（先检测 → 给结论 → 用户决定是否安装）。两个脚本按系统分开：
# 各自做系统分支守卫（在 macOS 上跑 Linux 版会直接告诉你该用哪个），
# 且**只读**——不写文件、不装软件、不改系统配置，装不装由用户决定。
@router.get("/preflight_macos.sh", include_in_schema=False)
async def preflight_macos() -> FileResponse:
    return _serve("preflight_macos.sh", "text/x-shellscript")


@router.get("/preflight_linux.sh", include_in_schema=False)
async def preflight_linux() -> FileResponse:
    return _serve("preflight_linux.sh", "text/x-shellscript")
