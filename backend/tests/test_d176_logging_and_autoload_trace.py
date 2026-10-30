"""D176（公网实测发现）：业务日志被静默丢弃 + 自动加载的技能没进执行流水。

两条都是"公网上才暴露"的缺陷——本地/单测都看不出来：

① **业务日志静默丢弃**：`setup_logging()` 此前**只在 LOG_JSON=true 时配置根 logger**，
   否则根 logger 没有 handler → 应用自身的 INFO 日志（技能自动路由命中、LLM 降级、
   向量通道降级、告警投递失败……）被 Python 丢弃，容器里只剩 uvicorn 的访问日志。
   公网排查"技能自动路由命中"为什么没出现，根因就在这——**日志不是没有，是根本没输出**。

② **自动路由的技能没进执行流水**：P1 上线后技能的主要使用路径是"自动注入"，
   而执行流水只记 LLM 主动 load_skill 的步骤 → P4 效果指标会把**用得最多的技能**
   算成"从没被用过"（口径失真，效果列不可信）。
   现在自动路由的技能也写成 `tool_name='load_skill'`（带 auto=true 标记），
   与手动加载同表同口径。

守卫锁住两条，别再退化。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "backend" / "app" / "main.py"
RCA = ROOT / "backend" / "app" / "services" / "rca.py"


def test_root_logging_always_configured():
    """根 logger 必须**总是**被配置——不能只在 LOG_JSON=true 时配置。

    回归锁：一旦有人把 setup_logging 改回"未开启 JSON 就 return"，业务日志会静默消失。
    """
    src = MAIN.read_text(encoding="utf-8")
    seg = src[src.index("def setup_logging"):src.index("\nsetup_logging()")]
    assert "if not get_settings().log_json:" not in seg, (
        "setup_logging 不得在 LOG_JSON 未开启时直接 return——那会让应用业务日志被静默丢弃")
    assert "root.addHandler(handler)" in seg, "根 logger 必须挂上 handler"
    assert "root.setLevel(logging.INFO)" in seg, "根 logger 级别必须是 INFO（业务日志是 INFO 级）"
    # 可读格式（非 JSON）也要带 request_id，便于按 ID 串起一次请求的全部日志
    assert "_PlainFormatter" in src, "缺少可读日志格式器（非 JSON 模式）"
    assert "current_request_id()" in src, "可读日志应带 request_id"


def test_uvicorn_dedup_in_both_modes():
    """uvicorn 自带 handler 要摘掉（否则访问日志打两遍）。"""
    src = MAIN.read_text(encoding="utf-8")
    seg = src[src.index("def setup_logging"):src.index("\nsetup_logging()")]
    assert 'lg.handlers, lg.propagate = [], True' in seg, "uvicorn handler 必须交给根 logger 统一输出"


def test_auto_loaded_skills_recorded_into_execution_steps():
    """自动路由的技能必须写进执行流水（否则 P4 效果指标口径失真）。"""
    src = RCA.read_text(encoding="utf-8")
    assert "_record_auto_loaded" in src, "rca.py 缺自动加载留痕函数"
    # 调用点必须在自动路由命中分支里
    idx = src.index("auto = await skills_svc.match_skills")
    seg = src[idx:idx + 1200]
    assert "_record_auto_loaded(" in seg, "自动路由命中后必须调用留痕函数"
    # 留痕用 load_skill 口径（P4 效果指标按 tool_name='load_skill' 统计）
    assert 'tool_name="load_skill"' in src, "自动加载应记为 load_skill（P4 口径一致）"
    assert '"auto": True' in src, "自动加载应带 auto 标记（与手动加载可区分）"


# ── D177：只读白名单是前缀判定 → 串联命令必须在平台侧先拦（D176 真机实测暴露）──────
def test_chained_commands_rejected_platform_side():
    """`df -h && curl …` / `df -h; rm -rf /` 不能因"首词命中白名单"而被判成只读。

    真机实测：LLM 拼了 `df -i && echo … && du … | tail`，平台放行、Agent 拦下，
    报错却说"目标机拒绝"——模型以为机器坏了，多花一轮。现在平台侧先拦并说清原因。
    """
    from app.services.rca_tools import authorize_readonly

    for bad in ["df -i && echo x && du -sh /var/log | tail",
                "df -h; rm -rf /tmp/x",
                "df -h || true",
                "df -h `id`",
                "df -h $(id)"]:
        ok, why = authorize_readonly(bad, allowed=["df -h"])
        assert ok is False, f"串联命令必须被拒：{bad!r}"
        assert "串联" in why, f"拒绝原因要说清是串联问题：{why}"
    # 普通管道两边都允许（Agent 也允许），不能因此误伤
    ok, _ = authorize_readonly("df -h | sort -h", allowed=[])
    assert ok is True, "普通管道应放行（与 Agent 侧口径一致）"
    # 技能授权路径仍然有效
    ok2, why2 = authorize_readonly('mysql --version', allowed=['mysql --version'])
    assert ok2 is True, why2
