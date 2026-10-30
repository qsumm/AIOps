"""D108 守卫：L3/L4 自动恢复用到的 3 个新动作——白名单放行合法命令、拦截越界。

不只是在白名单层校验，还包括 Agent 端的字符集/路径白名单模拟（防止平台层漏判时 Agent 兜底）。
"""
import re
from app.services.runbook import action_allowlist, match_action_allowlist

ALLOW = {it["id"] for it in action_allowlist()}


def test_l3_l4_actions_in_allowlist():
    """L3/L4 必需的 3 个新动作已在白名单内。"""
    for need in ("unlink-nginx-conf", "unlink-mariadb-cnf", "nginx-reload-safe"):
        assert need in ALLOW, f"白名单缺 {need}（L3/L4 必需）"


def test_l3_l4_actions_match_legal_commands():
    cases = [
        ("unlink-nginx-conf",   "rm -f /etc/nginx/conf.d/bad.conf"),
        ("unlink-nginx-conf",   "rm /etc/nginx/conf.d/old_upstream.conf"),
        ("unlink-mariadb-cnf",  "rm -f /etc/my.cnf.d/wrong_max_conn.cnf"),
        ("nginx-reload-safe",    "nginx -t && nginx -s reload"),
    ]
    for action_id, cmd in cases:
        hit = match_action_allowlist(cmd)
        assert hit and hit["id"] == action_id, f"应该匹配 {action_id} 但匹配 {hit}"


def test_l3_l4_actions_reject_unsafe_paths():
    """关键：路径越界必须被白名单拒——这是 L3/L4 的安全底线。"""
    unsafe = [
        "rm /etc/passwd",
        "rm -f /etc/shadow",
        "rm /etc/nginx/nginx.conf",                   # conf.d 子目录外
        "rm -f /etc/nginx/conf.d/../../../etc/passwd",  # 路径穿越（正则锚定 ^/etc/nginx/ 防）
        "rm -f /etc/sudoers.d/something",
        "rm -rf /etc/nginx/conf.d/",                    # 通配符目录
    ]
    for cmd in unsafe:
        hit = match_action_allowlist(cmd)
        assert hit is None, f"危险命令被放行：{cmd}（命中 {hit}）"


def test_l3_l4_reject_directory_traversal_in_filename():
    """Agent 端在 run_action 里再次校验文件名（{file} 不接受 ../）——这里复测白名单层。
    实际 run_action 用 task_filename_ok 强校验 + 路径 realpath startswith 目录前缀。
    此处用单测覆盖白名单正则的兜底。"""
    # 路径前缀锚定 ^/etc/nginx/conf.d/ 实际就拒了穿越
    assert match_action_allowlist("rm -f /etc/nginx/conf.d/../passwd") is None


def test_l3_l4_regex_anchored():
    """白名单正则必须 ^ 开头 $ 结尾——避免被宽松匹配意外放行危险命令。"""
    for it in action_allowlist():
        if it["id"] in ("unlink-nginx-conf", "unlink-mariadb-cnf", "nginx-reload-safe"):
            assert it["pattern"].startswith("^") and it["pattern"].endswith("$"), \
                f"{it['id']} 正则未锚定：{it['pattern']}"
