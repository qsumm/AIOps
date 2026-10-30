"""测试环境全局设置：关闭会对真实 LLM 发起调用的开关（保证单测快且确定）。"""
import os

os.environ.setdefault("CLUSTER_LLM_ENABLED", "false")
os.environ.setdefault("ALERT_AI_SUMMARY_ENABLED", "false")   # FR-C6：单测不真调 LLM

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()


# ── 全局 subprocess 污染守卫（D130 查案实录）─────────────────────────────
#
# 事故：`test_d121_self_upgrade` 里有一行 **裸赋值**
#     `m.subprocess.Popen = lambda cmd, **k: calls.append(cmd)`
# 而 `m.subprocess` 就是**全局 subprocess 模块**——裸赋值永不复原。
# 后果：该测试之后，整个会话里 `subprocess.run` 全部炸
#     （`subprocess.run` 内部是 `with Popen(...) as process:`，
#      而那个 lambda 返回 `list.append` 的 None → "NoneType 不支持上下文管理器"）。
#     受害的包括 **D3 前端语法门禁**（`test_d3_frontend_assets`）——
#     也就是项目最关键的那道前端防线，在完整回归里**静默失效**——
#     外加 11 条 preflight 用例。
#
# 为什么值得加这道守卫：它不是"又一条测试"，而是**堵住一整类**
# 「测试悄悄改写全局标准库、让后续所有依赖它的守卫集体失效」的坑。
# 任何测试若要改 subprocess，必须走 monkeypatch（自动还原）或自己 try/finally 还原。
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _guard_global_subprocess_intact():
    """每个用例结束后核对：全局 subprocess.run / Popen 必须已还原为原对象。

    顺序说明：本守卫是 autouse fixture → **最先 setup、最后 teardown**；
    而 monkeypatch 的 undo 发生在它之前，所以"用 monkeypatch 改写"是合法的，
    只有**裸赋值不还原**才会被这里逮到。
    """
    orig_run = subprocess.run
    orig_popen = subprocess.Popen
    yield
    assert subprocess.run is orig_run, (
        "本用例把全局 subprocess.run 改坏了且没还原——后续所有用 subprocess 的测试"
        "（含 D3 前端语法门禁）都会连带失效。请改用 monkeypatch.setattr。")
    assert subprocess.Popen is orig_popen, (
        "本用例把全局 subprocess.Popen 改坏了且没还原——后续所有用 subprocess 的测试"
        "（含 D3 前端语法门禁）都会连带失效。请改用 monkeypatch.setattr。")
