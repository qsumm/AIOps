"""通知传输层映射单测（ADR-002）：友好配置 → apprise URL。"""
from app.services.notify_transport import to_apprise_url


def test_feishu_full_webhook():
    url = to_apprise_url("feishu", {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc123-DEF"})
    assert url == "feishu://abc123-DEF/"


def test_feishu_bare_token():
    assert to_apprise_url("feishu", {"webhook_url": "abc123"}) == "feishu://abc123/"


def test_dingtalk_carries_sign_secret():
    # 关键回归：自研版忽略加签（ADR-002 §1 bug#2），apprise 必须带上 secret
    url = to_apprise_url("dingtalk", {
        "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=tok123&secret=sec456"})
    assert url == "dingtalk://tok123/sec456/"


def test_dingtalk_without_secret():
    url = to_apprise_url("dingtalk", {"webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=tok123"})
    assert url == "dingtalk://tok123//"


def test_email_port_465_implicit_tls():
    # 关键回归：自研版在 465 用 starttls（ADR-002 §1 bug#1）；端口信息必须原样传给 apprise
    url = to_apprise_url("email", {"smtp": {"host": "smtp.exmail.qq.com", "port": 465,
                                            "user": "ops@x.com", "pass": "p", "to": "me@x.com"}})
    assert url == "mailto://ops@x.com:p@smtp.exmail.qq.com:465?to=me@x.com"


def test_webhook_scheme_maps_https_to_jsons():
    assert to_apprise_url("webhook", {"webhook_url": "https://hook.example.com/alert"}) == "jsons://hook.example.com/alert"
    assert to_apprise_url("webhook", {"webhook_url": "http://10.0.0.1:9000/alert"}) == "json://10.0.0.1:9000/alert"


def test_escape_hatch_apprise_url_wins():
    # 逃生口：用户直接填 apprise URL 时透传（可接 apprise 支持的 150+ 渠道）
    url = to_apprise_url("webhook", {"apprise_url": "slack://token/a/b/c", "webhook_url": "https://ignored"})
    assert url == "slack://token/a/b/c"


def test_incomplete_config_returns_none():
    assert to_apprise_url("feishu", {}) is None
    assert to_apprise_url("email", {"smtp": {"host": "h"}}) is None
    assert to_apprise_url("webhook", {}) is None
    assert to_apprise_url("unknown-type", {"x": 1}) is None
