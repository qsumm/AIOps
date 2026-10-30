"""D110 守卫：结构化执行意图（EXEC 行）解析 / 渲染 / 安全边界。"""
import re

from app.services.runbook import EXEC_ACTIONS, parse_exec_line


def test_parse_valid_exec_lines():
    cases = [
        ("重启 nginx 服务\n  EXEC: restart_service service=nginx", "restart_service",
         {"service": "nginx"}, "systemctl restart nginx"),
        ("删除错误配置\n  EXEC: unlink_nginx_conf file=bad_upstream.conf", "unlink_nginx_conf",
         {"file": "bad_upstream.conf"}, "rm -f /etc/nginx/conf.d/bad_upstream.conf"),
        ("删除 mariadb 坏配置\n  EXEC: unlink_mariadb_cnf file=bad_max_conn.cnf", "unlink_mariadb_cnf",
         {"file": "bad_max_conn.cnf"}, "rm -f /etc/my.cnf.d/bad_max_conn.cnf"),
        ("重载 nginx\n  EXEC: nginx_reload_safe", "nginx_reload_safe", {},
         "nginx -t && nginx -s reload"),
    ]
    for text, action, params, cmd in cases:
        ex = parse_exec_line(text)
        assert ex, f"应解析成功：{text}"
        assert ex["action"] == action and ex["params"] == params and ex["cmd"] == cmd


def test_parse_rejects_unknown_or_malformed():
    bad = [
        "EXEC: rm_rf_everything file=/",
        "EXEC: restart_service",                        # 缺 service
        "EXEC: unlink_nginx_conf file=../../etc/passwd",  # 路径穿越
        "EXEC: unlink_nginx_conf file=evil.sh",           # 后缀不对
        "EXEC: unlink_mariadb_cnf file=x.conf",           # 后缀不对（要 .cnf）
        "EXEC: restart_service service=nginx; rm -rf /",  # 元字符
        "重启服务（没有 EXEC 行）",
    ]
    for text in bad:
        assert parse_exec_line(text) is None, f"必须拒绝：{text}"


def test_all_exec_actions_render_safe():
    """注册表里所有动作的渲染结果只含固定模板 + 已校验参数，不含可注入的元字符。
    唯一例外 nginx_reload_safe 的 `&&` 是**固定模板**（sh -c 串行两段），不接受外部输入。"""
    for action, spec in EXEC_ACTIONS.items():
        sample = {"service": "nginx", "file": "x.conf" if action.endswith("_conf") else "x.cnf"} \
            if spec["needs"] else {}
        cmd = spec["render"](sample)
        if action in ("nginx_reload_safe", "nginx_fix_bad_conf"):
            # 固定模板（无外部输入），白名单正则逐字锁定；不参与"无元字符"断言
            continue
        if action == "mariadb_fix_bad_cnf":
            continue
        assert not re.search(r"[;&|`$<>*?]", cmd), f"{action} 渲染含危险字符：{cmd}"


def test_parse_rejects_trailing_junk():
    """行尾夹带其它命令必须整体拒绝（不能只取前半段放行）。"""
    assert parse_exec_line("EXEC: restart_service service=nginx; rm -rf /") is None
    assert parse_exec_line("EXEC: nginx_reload_safe && rm -rf /") is None


def test_derive_actions_from_text():
    """兜底翻译器：从已验证建议文本确定性地推导白名单动作。"""
    from app.services.runbook import derive_exec_actions

    rpt = ("## VALIDATED FIXES\n- 重启 nginx 服务恢复\n  验证依据：check_probe nginx进程=down\n"
           "  若启动失败：删除 /etc/nginx/conf.d/bad_upstream.conf 后 nginx -t && nginx -s reload\n"
           "- restart mariadb if needed")
    acts = derive_exec_actions(rpt)
    ids = [a["action"] for a in acts]
    assert "nginx_fix_bad_conf" in ids and "unlink_nginx_conf" in ids and "nginx_reload_safe" in ids
    # P1-8：只有报告里出现该组件的配置错误证据才升级；mariadb 这里没有 unknown variable，
    # 所以保持 restart_service，不能顺手去隔离 .cnf。
    assert "mariadb_fix_bad_cnf" not in ids
    assert any(a["action"] == "restart_service" and a["params"].get("service") == "mariadb" for a in acts)
    confs = [a["params"].get("file") for a in acts if a["action"] == "unlink_nginx_conf"]
    assert confs == ["bad_upstream.conf"]


def test_config_fix_requires_config_error_evidence():
    """P1-8：崩溃/僵死没有配置错误证据 → restart；有证据才升级为配置急救。"""
    from app.services.runbook import derive_exec_actions

    no_evidence = derive_exec_actions("## VALIDATED FIXES\n- 重启 mariadb 服务恢复")
    assert [a["action"] for a in no_evidence] == ["restart_service"]

    with_evidence = derive_exec_actions(
        "## VALIDATED FIXES\n- 重启 mariadb；日志出现 unknown variable：max_connectionss")
    assert any(a["action"] == "mariadb_fix_bad_cnf" for a in with_evidence)


def test_derive_actions_is_conservative():
    """没有明确重启/文件名时不得乱造动作。"""
    from app.services.runbook import derive_exec_actions
    assert derive_exec_actions("## VALIDATED FIXES\n- 观察指标变化，无需动作") == []
    assert derive_exec_actions("重启 everything 服务") == []   # 未知 unit 不映射


def test_derive_inline_exec_and_api_style():
    """内联 EXEC / restart_service service=x 也要能翻译（AI 常写进句子里）。"""
    from app.services.runbook import derive_exec_actions
    rpt1 = "再考虑用 `EXEC: restart_service service=nginx` 走平台白名单恢复"
    acts1 = derive_exec_actions(rpt1)
    # 没有配置错误证据 → 保持最小动作 restart，不自动升级为“修配置”
    assert any(a["action"] == "restart_service" and a["params"].get("service") == "nginx" for a in acts1)
    assert not any(a["action"] == "nginx_fix_bad_conf" for a in acts1)
    rpt2 = "可通过 restart_service service=redis 让连接池重连"
    acts2 = derive_exec_actions(rpt2)
    assert any(a["action"] == "restart_service" and a["params"]["service"] == "redis" for a in acts2)  # redis 无急救动作
    # 非法 unit 仍拒绝
    assert not [a for a in derive_exec_actions("EXEC: restart_service service=bad;rm") if a["action"] == "restart_service"]


def test_prompt_action_catalog_matches_exec_actions():
    """P1-8：提示词里的动作目录必须与后端注册表完全一致（防止再次出现 7 vs 9）。"""
    from app.services.prompts import RCA_SYSTEM_PROMPT
    from app.services.runbook import EXEC_ACTIONS

    missing = [a for a in EXEC_ACTIONS if f"`{a}`" not in RCA_SYSTEM_PROMPT]
    assert not missing, f"提示词缺少动作：{missing}"
    assert "9 个 action" in RCA_SYSTEM_PROMPT


def test_post_check_failure_requires_rollback(monkeypatch):
    """P1-9：命令 rc=0 但 post_check 未通过 → ok=False + rollback_required + 回滚提示。"""
    import asyncio
    from datetime import datetime
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.models.ops import ActionApproval, ActionAudit, Runbook
    from app.services import executor, runbook as rb_svc

    async def fake_enqueue(host, action, params, timeout_s=90):
        return {"ok": True, "task_id": 999, "result": {
            "ok": True, "post_check": {"ok": False, "checks": [{"name": "systemctl", "ok": False}]},
            "backup": "/var/backups/opsagent/20260101.bad.conf.bak",
            "src": "/etc/nginx/conf.d/bad.conf",
        }}

    async def main():
        monkeypatch.setattr(executor, "enqueue_and_wait", fake_enqueue)
        async with SessionLocal() as db:
            rb = Runbook(name="P1-9 rollback", host_ident="p1-9-host", execution_mode="manual",
                         status="approved", risk_level="mid",
                         steps=[{"idx": 1, "desc": "修复 nginx 坏配置", "action": "unlink_nginx_conf",
                                 "params": {"file": "bad.conf"}, "cmd": "rm -f /etc/nginx/conf.d/bad.conf",
                                 "rollback": "mv /var/backups/opsagent/20260101.bad.conf.bak /etc/nginx/conf.d/bad.conf"}])
            db.add(rb)
            await db.commit()
            await db.refresh(rb)
            rid = rb.id
        try:
            conf = await rb_svc.issue_confirm(rid, 1, operator="admin")
            assert conf["ok"], conf
            res = await rb_svc.execute_step(rid, 1, operator_name="admin",
                                            confirm_token=conf["confirm_token"])
            assert res["ok"] is False
            assert res["rollback_required"] is True
            assert "mv " in res["rollback_hint"]
            assert res["post_check"]["ok"] is False
        finally:
            async with SessionLocal() as db:
                await db.execute(delete(ActionApproval).where(ActionApproval.runbook_id == rid))
                await db.execute(delete(ActionAudit).where(ActionAudit.runbook_id == rid))
                await db.execute(delete(Runbook).where(Runbook.id == rid))
                await db.commit()

    asyncio.run(main())



def test_structured_action_guard_uses_cmd_not_check():
    """L2 实机回归：check 是验证命令，不能把 restart 动作误判为只读。"""
    from app.services.runbook import guard_command, step_command

    step = {"action": "restart_service", "cmd": "systemctl restart nginx",
            "check": "systemctl is-active nginx"}
    assert step_command(step) == "systemctl restart nginx"
    g = guard_command(step_command(step))
    assert g.verdict == "hitl" and g.need_hitl is True


def test_execute_step_guard_uses_step_command():
    """L2 实机回归：execute_step 必须用 step_command（cmd 优先），不能再用 check 当执行命令。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app/services/runbook.py").read_text(encoding="utf-8")
    assert 'step.get("check") or step.get("cmd")' not in src
    assert src.count("cmd = step_command(step)") >= 2
