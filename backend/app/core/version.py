"""版本信息单一来源（D66）。

为什么要单独一个模块：版本号此前散落在登录页写死的字符串（`AIOPS V1.0.0 · BUILD 0059`）
与文档里，改一处忘一处必然对不上。现在**唯一来源是仓库根的 `VERSION` 文件**：
  · 后端：本模块读取并在 `/api/health` 暴露（前端左下角版本号从接口取）
  · 发布：`VERSION` 与《版本发布说明》最新版号的一致性由回归锁保证
  · 构建号：镜像构建时用 `--build-arg GIT_SHA=$(git rev-parse --short HEAD)` 注入；
    未注入时显示 `dev`（不阻塞本地开发与测试）
读取顺序（先到先得）：
  1. 环境变量 APP_VERSION（容器/CI 显式覆盖）
  2. 镜像内 /app/VERSION（Dockerfile COPY 落在这里）
  3. 仓库根 VERSION（本地开发）
  4. 兜底 "dev"
"""
from __future__ import annotations

import os
from pathlib import Path

_CANDIDATES = (
    Path("/app/VERSION"),                                        # 镜像内（Dockerfile COPY）
    Path(__file__).resolve().parents[3] / "VERSION",             # 仓库根（backend/app/core/version.py → 上溯 3 层）
)


def _read_first(paths) -> str:
    for p in paths:
        try:
            if p.exists():
                v = p.read_text(encoding="utf-8").strip()
                if v:
                    return v
        except OSError:
            continue
    return ""


def app_version() -> str:
    """产品版本号（形如 `1.1.13`），取不到时 `dev`。"""
    return os.getenv("APP_VERSION", "").strip() or _read_first(_CANDIDATES) or "dev"


def build_sha() -> str:
    """构建对应的 git 短提交号；未注入时 `dev`。"""
    return os.getenv("GIT_SHA", "").strip() or "dev"


def build_time() -> str:
    """构建时间（ISO 8601，UTC）；未注入时空串（前端显示 `-`）。"""
    return os.getenv("BUILD_TIME", "").strip()


def version_payload() -> dict:
    """`/api/health` 与版本信息页共用的载荷。"""
    return {"version": app_version(), "build": build_sha(), "build_time": build_time()}
