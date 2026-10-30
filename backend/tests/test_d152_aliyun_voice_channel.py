"""D152 守卫：阿里云语音通知渠道（用户需求：已在阿里云开通语音服务，要能打电话播报告警）。

背景：apprise 生态的语音插件只有 twilio / telnyx / sinch / voipms 四个国际网关，
**没有阿里云**。阿里云语音（SingleCallByTts）是带签名的 RPC，不是 URL 订阅 → 必须自研直连。

★ 本文件里的「接口事实」全部来自**官方 SDK 源码**（aliyun-python-sdk 的
  dyvmsapi-20170525 + alibabacloud-tea-openapi），不是凭记忆：
  - 接入点 dyvmsapi.aliyuncs.com（**不是** dysapi.aliyuncs.com——那是短信服务的域名前缀）
  - 业务参数 CalledNumber / TtsCode / TtsParam / CalledShowNumber / PlayTimes / Speed / Volume
    （**不是** PhoneNumber / TemplateCode——那俩是短信服务的参数名）
  - 响应体 {"Code":"OK","Message":"OK","RequestId":…,"CallId":…}，失败时 4xx/5xx 且 body 带 Code/Message
  - 签名 = RPC v1.0（HMAC-SHA1），与官方 SDK 的 Utils.get_rpcsignature 逐字节一致

本文件锁死五件事：
  ① 签名算法必须与官方 SDK **逐字节一致**（签名错了是 SignatureDoesNotMatch，且极难一眼看出根因）；
  ② 请求必须打在 dyvmsapi.aliyuncs.com，用 SingleCallByTts / Version=2017-05-25，
     且**不能混用短信服务的参数名**（PhoneNumber/TemplateCode）；
  ③ TtsParam 的 key 必须可配（= 模板正文里的变量名）+ 内容就是告警正文；
  ④ 配置不完整 / 参数越界 → **不发出请求**，直接给人话错误（死渠道不能建）；
  ⑤ aliyun_voice 算「电话类渠道」→ S1 分级通知必须认它。

**不调用真实阿里云**：所有出网请求都被 monkeypatch 掉（只断言请求长什么样）。
真要验联通性，用界面上的「测试」按钮打真实电话。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import pathlib
import urllib.parse

import pytest
from httpx import ASGITransport, AsyncClient

ROOT = pathlib.Path(__file__).resolve().parents[2]

FULL_CFG = {
    "access_key_id": "LTAI5tTEST",
    "access_key_secret": "testsecret",
    "tts_code": "TTS_123456789",
    "phone": "13800138000",
}

# 官方 SDK / 文档事实（改动这里前先回去核对 SDK 源码）
SDK_ENDPOINT = "dyvmsapi.aliyuncs.com"
SDK_ACTION = "SingleCallByTts"
SDK_VERSION = "2017-05-25"


# ───────────────────────── ① 签名算法（对拍官方 SDK） ─────────────────────────
def _sdk_percent_encode(s: str) -> str:
    """aliyun-python-sdk-core / acs/signature.py 的 percent_encode（逐行照抄）。"""
    ret = urllib.parse.quote(s, safe="")
    return ret.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


def _sdk_compose_string_to_sign(method: str, queries: dict[str, str]) -> str:
    """aliyun-python-sdk-core / acs/signature.py 的 compose_string_to_sign（逐行照抄）。"""
    cqs = ""
    for k, v in sorted(queries.items()):
        cqs += _sdk_percent_encode(k) + "=" + _sdk_percent_encode(v) + "&"
    cqs = cqs[0:-1]
    return method + "&" + _sdk_percent_encode("/") + "&" + _sdk_percent_encode(cqs)


def _sdk_sign(secret: str, string_to_sign: str) -> str:
    h = hmac.new((secret + "&").encode(), string_to_sign.encode(), hashlib.sha1)
    return base64.b64encode(h.digest()).decode()


def test_signature_matches_official_sdk():
    """签名必须与官方 SDK 完全一致（不是"看起来对"，是逐字节相同）。"""
    from app.services.notify_transport import _aliyun_sign

    cases = [
        ({"Action": "SingleCallByTts", "CalledNumber": "13800138000",
          "TtsCode": "TTS_1", "TtsParam": '{"content":"hi"}', "Format": "JSON"}, "testsecret"),
        ({"AccessKeyId": "testId", "Action": "DescribeRegions", "Format": "JSON",
          "RegionId": "cn-hangzhou", "SignatureMethod": "HMAC-SHA1",
          "SignatureNonce": "5f0f1f10-d50f-4e2e-8ba3-e0e2e2e2e2e2",
          "SignatureVersion": "1.0", "Timestamp": "2018-01-01T12:00:00Z",
          "Version": "2014-05-26"}, "testsecret"),
    ]
    for params, secret in cases:
        mine = _aliyun_sign(secret, params)
        expect = _sdk_sign(secret, _sdk_compose_string_to_sign("GET", params))
        assert mine == expect, f"签名与官方 SDK 不一致：{mine} != {expect}"


def test_percent_encode_edge_cases():
    """percent-encode 的坑：默认 safe='/' 会漏掉 /，且 + 不能当空格。"""
    from app.services.notify_transport import _aliyun_percent_encode

    for s in ["hello world", "a*b", "a~b", "中文内容", "a+b", "a/b", "a&b=c", '{"content":"x y"}']:
        assert _aliyun_percent_encode(s) == _sdk_percent_encode(s), s


def test_signature_contains_no_signature_param():
    """签名只对除 Signature 外的参数计算（把 Signature 一起签进去必然错）。"""
    from app.services.notify_transport import _aliyun_sign

    p = {"A": "1", "B": "2"}
    assert _aliyun_sign("sec", p) == _aliyun_sign("sec", {**p, "Signature": "xxx"})


# ───────────────────────── ② 请求构造（对照官方 SDK 的事实） ─────────────────────────
def _capture(monkeypatch, resp: dict | None = None, status: int = 200):
    """把 httpx 打桩，返回 (sent_query, send_aliyun_voice 的结果)。"""
    import asyncio

    from app.services import notify_transport as nt

    seen = {}

    class _Resp:
        def __init__(self):
            self.status_code = status

        def json(self):
            if resp is None:
                raise ValueError("not json")
            return resp

    class _Client:
        def __init__(self, *a, **kw):
            seen["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen["url"] = url
            return _Resp()

    monkeypatch.setattr(nt.httpx, "AsyncClient", _Client)
    return seen


@pytest.mark.asyncio
async def test_request_shape_matches_official_sdk(monkeypatch):
    """请求必须打在 dyvmsapi.aliyuncs.com，参数名与官方 SDK 一致，且不混用短信服务参数名。"""
    from app.services import notify_transport as nt

    seen = _capture(monkeypatch, resp={"Code": "OK", "Message": "OK", "RequestId": "r-1", "CallId": "c-9"})

    ok, detail = await nt.send_aliyun_voice(FULL_CFG, "S1 紧急告警", "主机 h1 CPU 使用率 98%")
    assert ok, detail
    assert "CallId=c-9" in detail, detail

    url = seen["url"]
    assert url.startswith(f"https://{SDK_ENDPOINT}?"), f"接入点不对：{url[:80]}"
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["Action"] == [SDK_ACTION]
    assert q["Version"] == [SDK_VERSION]
    assert q["Format"] == ["JSON"]
    assert q["SignatureMethod"] == ["HMAC-SHA1"]
    assert q["SignatureVersion"] == ["1.0"]
    assert q["AccessKeyId"] == ["LTAI5tTEST"]
    assert q["Signature"] and q["Signature"][0]
    assert q["Timestamp"][0].endswith("Z")

    # ★ 业务参数名：官方 SDK 是 CalledNumber / TtsCode，不是 PhoneNumber / TemplateCode
    assert q["CalledNumber"] == ["13800138000"]
    assert q["TtsCode"] == ["TTS_123456789"]
    assert "PhoneNumber" not in q, "混用了短信服务的参数名 PhoneNumber"
    assert "TemplateCode" not in q, "混用了短信服务的参数名 TemplateCode"

    # TtsParam 是 JSON 字符串，key = 模板变量名，value = 告警正文
    tts = json.loads(q["TtsParam"][0])
    assert list(tts) == ["content"]
    assert "S1 紧急告警" in tts["content"]
    assert "CPU 使用率 98%" in tts["content"]


@pytest.mark.asyncio
async def test_optional_params_passthrough(monkeypatch):
    """主叫显示号 / 播放次数 / 音量 / 语速按官方 SDK 的名字透传。"""
    from app.services import notify_transport as nt

    seen = _capture(monkeypatch, resp={"Code": "OK"})
    ok, _ = await nt.send_aliyun_voice(
        {**FULL_CFG, "called_show_number": "4001234567",
         "play_times": 2, "volume": 80, "speed": -100},
        "t", "b")
    assert ok
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert q["CalledShowNumber"] == ["4001234567"]
    assert q["PlayTimes"] == ["2"]
    assert q["Volume"] == ["80"]
    assert q["Speed"] == ["-100"]


@pytest.mark.asyncio
async def test_optional_params_out_of_range_are_dropped(monkeypatch):
    """越界的播放参数不发给阿里云（服务端校验会先 400，这里是兜底）。"""
    from app.services import notify_transport as nt

    seen = _capture(monkeypatch, resp={"Code": "OK"})
    ok, _ = await nt.send_aliyun_voice(
        {**FULL_CFG, "play_times": 9, "volume": 999, "speed": 99999}, "t", "b")
    assert ok
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    for k in ("PlayTimes", "Volume", "Speed"):
        assert k not in q, f"越界的 {k} 不该发出去"


@pytest.mark.asyncio
async def test_tts_param_key_is_configurable(monkeypatch):
    """模板变量名可配——用户在阿里云把变量起成别的名字时，平台要能跟着变。

    TtsParam 的 key 必须与模板正文里的变量名一致，否则播报的是模板固定文案。
    """
    from app.services import notify_transport as nt

    seen = _capture(monkeypatch, resp={"Code": "OK"})
    ok, _ = await nt.send_aliyun_voice(
        {**FULL_CFG, "tts_param_key": "alarm_text"}, "t", "b")
    assert ok
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    tts = json.loads(q["TtsParam"][0])
    assert list(tts) == ["alarm_text"]


@pytest.mark.asyncio
async def test_out_id_passthrough(monkeypatch):
    """外部流水号要透传（阿里云回执里会带回，可用来筛这批呼叫）。"""
    from app.services import notify_transport as nt

    seen = _capture(monkeypatch, resp={"Code": "OK"})
    ok, _ = await nt.send_aliyun_voice({**FULL_CFG, "out_id": "aiops-s1"}, "t", "b")
    assert ok
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert q["OutId"] == ["aiops-s1"]


@pytest.mark.asyncio
async def test_out_id_too_long_not_sent(monkeypatch):
    """超 15 字节的流水号不发给阿里云（官方限制；保存时已 400，这里是兜底）。"""
    from app.services import notify_transport as nt

    seen = _capture(monkeypatch, resp={"Code": "OK"})
    ok, _ = await nt.send_aliyun_voice({**FULL_CFG, "out_id": "x" * 20}, "t", "b")
    assert ok
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert "OutId" not in q


def test_all_sdk_business_params_covered():
    """参数覆盖面守卫：**官方 SDK 的 SingleCallByTtsRequest 里，凡是普通用户该填的，
    平台都得有地方填**（能填 → 会发）。

    来源是官方 SDK 源码的注释块：标了 "This parameter is required." 的是必填。
    跨账号/委托专用的三个（OwnerId / ResourceOwnerAccount / ResourceOwnerId）
    **有意不暴露**——普通用户填反而出错。
    """
    import pathlib
    import re

    sdk = pathlib.Path("/tmp/dyvms/dyvmsapi-20170525/alibabacloud_dyvmsapi20170525"
                       "/models/_single_call_by_tts_request.py")
    if not sdk.exists():       # 本机没克隆官方 SDK 时跳过（CI/交接环境）
        pytest.skip("未克隆官方 SDK 源码，无法做参数面对拍")

    src = sdk.read_text(encoding="utf-8")
    attr_to_key = {a: k for k, a in re.findall(r"result\['(\w+)'\] = self\.(\w+)", src)}
    assigns = list(re.finditer(r"^        self\.(\w+) = \w+$", src, re.M))
    required = set()
    for i, m in enumerate(assigns):
        start = assigns[i - 1].end() if i else 0
        if "This parameter is required." in src[start:m.start()]:
            required.add(attr_to_key[m.group(1)])

    # 前端表单能填的 config 键 → 平台发的 query 键
    fe = (ROOT / "static/js/pages/settings.js").read_text(encoding="utf-8")
    cands = [m.start() for m in re.finditer(r'if \(t === "aliyun_voice"\) \{', fe)]
    block = None
    for st in cands:
        en = fe.index('} else if (t === "phone")', st) if '} else if (t === "phone")' in fe[st:] else len(fe)
        cand = fe[st:en]
        if "cfgIn." in cand:
            block = cand
            break
    assert block, "前端 aliyun_voice 的 cfgIn 分支没找到"
    cfg_keys = set(re.findall(r"cfgIn\.(\w+)\s*=", block))

    # 必填：CalledNumber(phone) / TtsCode(tts_code) —— 必须有表单字段
    assert "phone" in cfg_keys, "缺被叫号码字段"
    assert "tts_code" in cfg_keys, "缺语音模板 Code 字段"
    assert "access_key_id" in cfg_keys and "access_key_secret" in cfg_keys, "缺凭据字段"
    # 必填集合里不能有我们没暴露的（跨账号的三个必填则不适用）
    deliberate = {"OwnerId", "ResourceOwnerAccount", "ResourceOwnerId"}
    for key in required:
        if key in deliberate:
            continue
        assert key in {"CalledNumber", "TtsCode"}, f"SDK 新增必填参数 {key} 平台未处理"


@pytest.mark.asyncio
async def test_error_code_is_surfaced_with_detail(monkeypatch):
    """阿里云返回非 OK 的 Code → 失败且 detail 含原因与 Detail（要写进通知台账）。"""
    from app.services import notify_transport as nt

    _capture(monkeypatch, resp={
        "Code": "isv.OUT_OF_SERVICE", "Message": "服务停机", "Detail": "模板不存在"}, status=400)
    ok, detail = await nt.send_aliyun_voice(FULL_CFG, "t", "b")
    assert not ok
    assert "阿里云语音拒绝" in detail
    assert "服务停机" in detail
    assert "模板不存在" in detail


@pytest.mark.asyncio
async def test_error_code_and_chinese_hint(monkeypatch):
    """错误必须带**可检索的错误码** + 中文解释 + 下一步。

    阿里云返回的 Message 是英文的，光丢英文等于让用户自己翻译自己猜；
    Code 则是唯一能拿去搜官方《API 错误码》文档的机器可读键。
    """
    from app.services import notify_transport as nt

    _capture(monkeypatch, resp={
        "Code": "InvalidAccessKeyId.NotFound",
        "Message": "Specified access key is not found."}, status=400)
    ok, detail = await nt.send_aliyun_voice(FULL_CFG, "t", "b")
    assert not ok
    assert "InvalidAccessKeyId.NotFound" in detail, "错误码没带出来"
    assert "AccessKey ID 不存在" in detail, "没有中文解释"
    assert "RAM" in detail, "没给出下一步"
    assert "Specified access key is not found." in detail, "英文原文没保留"


def test_error_map_covers_common_cases():
    """常见错误码要有中文解释；没收录的也要带 Code（至少可检索）。"""
    from app.services.notify_transport import _aliyun_voice_error

    for code in ("InvalidAccessKeyId.NotFound", "SignatureDoesNotMatch",
                 "Forbidden.RAM", "isv.TTS_PARAM_ILLEGAL", "isv.OUT_OF_SERVICE",
                 "isv.MOBILE_NUMBER_ILLEGAL", "isv.TEMPLATE_ILLEGAL"):
        out = _aliyun_voice_error(code, {"Message": "x"})
        assert code in out, code
        # 中文解释不能是英文原文
        assert out.split("：", 1)[-1] != "x", code
    # 未收录的错误码：仍要带 Code 与原文
    out = _aliyun_voice_error("isv.SOMETHING_NEW", {"Message": "brand new error"})
    assert "isv.SOMETHING_NEW" in out
    assert "brand new error" in out


@pytest.mark.asyncio
async def test_http_error_without_code(monkeypatch):
    """非 200 且 body 不是预期 JSON → 报 HTTP 状态码。"""
    from app.services import notify_transport as nt

    _capture(monkeypatch, resp=None, status=403)
    ok, detail = await nt.send_aliyun_voice(FULL_CFG, "t", "b")
    assert not ok
    assert "403" in detail


# ───────────────────────── ③ 配置校验（死渠道不能建） ─────────────────────────
def test_aliyun_voice_missing_fields():
    from app.services.notify_transport import _aliyun_voice_missing

    assert _aliyun_voice_missing({}) == [
        "AccessKey ID", "AccessKey Secret", "语音模板 Code", "被叫手机号"]
    assert _aliyun_voice_missing(FULL_CFG) == []


def test_validate_channel_aliyun_voice():
    """服务端校验：缺项 → 人话错误；齐全 → None。"""
    from app.services.notify_transport import validate_channel

    err = validate_channel("aliyun_voice", {"access_key_id": "x"})
    assert err and "AccessKey Secret" in err and "语音模板 Code" in err and "被叫手机号" in err
    assert validate_channel("aliyun_voice", FULL_CFG) is None


def test_to_apprise_url_aliyun_voice_is_none():
    """aliyun_voice 不走 apprise —— 即使配了 apprise_url 也必须返回 None。

    否则用户填了 apprise_url 会被当成 apprise 渠道投递，而 apprise 解析不了
    阿里云的签名串 → 投递静默失败。
    """
    from app.services.notify_transport import to_apprise_url

    assert to_apprise_url("aliyun_voice", {}) is None
    assert to_apprise_url("aliyun_voice", {"apprise_url": "json://x"}) is None
    assert to_apprise_url("aliyun_voice", FULL_CFG) is None


def test_friendly_types_includes_aliyun_voice():
    from app.services.notify_transport import FRIENDLY_TYPES

    assert "aliyun_voice" in FRIENDLY_TYPES


@pytest.mark.asyncio
async def test_send_channel_routes_aliyun_voice(monkeypatch):
    """send_channel 必须把 aliyun_voice 路由到自研直连（而不是 apprise）。"""
    from app.services import notify_transport as nt

    called = {}

    async def _fake(cfg, title, body):
        called["cfg"] = cfg
        return True, "aliyun-voice ok"

    monkeypatch.setattr(nt, "send_aliyun_voice", _fake)
    ok, detail = await nt.send_channel("aliyun_voice", FULL_CFG, "t", "b")
    assert ok and detail == "aliyun-voice ok"
    assert called["cfg"] == FULL_CFG


def test_send_aliyun_voice_text_truncated():
    """超长正文要截断（官方文档：模板中的变量总长 < 250 字符）。"""
    from app.services.notify_transport import _ALIYUN_VOICE_MAX_TEXT, _aliyun_voice_text

    text = _aliyun_voice_text("标题", "正文" * 500)
    assert len(text) <= _ALIYUN_VOICE_MAX_TEXT
    assert _ALIYUN_VOICE_MAX_TEXT < 250
    # 换行压成空格（TTS 念成逗号很难听）
    assert " " in _aliyun_voice_text("a", "b\nc") and "\n" not in _aliyun_voice_text("a", "b\nc")


@pytest.mark.asyncio
async def test_incomplete_config_does_not_call(monkeypatch):
    """配置不全 → 不发请求。"""
    from app.services import notify_transport as nt

    seen = _capture(monkeypatch, resp={"Code": "OK"})
    ok, detail = await nt.send_aliyun_voice({"access_key_id": "x"}, "t", "b")
    assert not ok
    assert "配置不完整" in detail
    assert "url" not in seen


# ───────────────────────── ④ API 层：CRUD + 掩码 + S1 认它 ─────────────────────────
async def _client():
    from app.core.security import get_current_user
    from app.db.session import SessionLocal
    from app.main import create_app
    from app.models.shell import User

    app = create_app()
    async with SessionLocal() as db:
        admin = await db.get(User, 1)
    app.dependency_overrides[get_current_user] = lambda: admin
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_channel_crud_and_masking():
    """建渠道 → 列表出参掩码 Secret → 编辑留空保留旧 Secret。"""
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.models.alert import NotificationChannel

    app, _ = await _client()
    name = "d152-aliyun"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/ops/notify-channels", json={
            "name": name, "type": "aliyun_voice", "config_json": dict(FULL_CFG)})
        assert r.status_code == 200, r.text[:200]
        cid = r.json()["data"]["id"]

        # 出参：Secret 必须整段掩码（不能像 webhook 那样露头露尾）
        r = await ac.get("/api/ops/notify-channels")
        item = [x for x in r.json()["data"]["items"] if x["name"] == name][0]
        assert item["type"] == "aliyun_voice"
        assert item["config_json"]["access_key_secret"] == "***"
        assert "testsecret" not in r.text, "Secret 泄露到出参了！"
        # 非凭据字段照常返回（用户要能核对配的是哪个 Key / 哪个号）
        assert item["config_json"]["access_key_id"] == "LTAI5tTEST"
        assert item["config_json"]["phone"] == "13800138000"

        # 编辑：Secret 留空（不传）→ 保留旧值；改手机号要生效
        r = await ac.put(f"/api/ops/notify-channels/{cid}", json={
            "name": name, "type": "aliyun_voice", "enabled": True,
            "config_json": {"phone": "13900139000"}})
        assert r.status_code == 200, r.text[:200]
        async with SessionLocal() as db:
            ch = await db.get(NotificationChannel, cid)
            assert ch.config_json["access_key_secret"] == "testsecret", "Secret 被覆盖/丢失了"
            assert ch.config_json["phone"] == "13900139000"
            assert ch.config_json["tts_code"] == "TTS_123456789"
            await db.execute(delete(NotificationChannel).where(NotificationChannel.id == cid))
            await db.commit()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_channel_rejects_incomplete_aliyun_voice():
    """配置不全 → 400 人话（不能建出永远发不出去的死渠道）。"""
    app, _ = await _client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/ops/notify-channels", json={
            "name": "d152-bad", "type": "aliyun_voice", "config_json": {"access_key_id": "x"}})
        assert r.status_code == 400, r.text[:200]
        assert "配置不完整" in r.text
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_channel_accepts_domestic_and_intl_phone():
    """国内 13800138000 与国际 +8613800138000 都要接受（阿里云两种都收）。"""
    app, _ = await _client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        for i, ph in enumerate(("13800138000", "+8613800138000", "0571-12345678")):
            r = await ac.post("/api/ops/notify-channels", json={
                "name": f"d152-ph{i}", "type": "aliyun_voice",
                "config_json": {**FULL_CFG, "phone": ph}})
            assert r.status_code == 200, f"{ph} 被拒：{r.text[:160]}"
            cid = r.json()["data"]["id"]
            await ac.delete(f"/api/ops/notify-channels/{cid}")
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_channel_rejects_bad_phone_format():
    """手机号明显不合法 → 400（格式错了要在保存时就说清）。"""
    app, _ = await _client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/ops/notify-channels", json={
            "name": "d152-bad2", "type": "aliyun_voice",
            "config_json": {**FULL_CFG, "phone": "abc"}})
        assert r.status_code == 400, r.text[:200]
        assert "格式" in r.text
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_channel_rejects_out_of_range_playback_params():
    """播放次数/音量/语速越界 → 400（不能悄悄丢掉，用户以为生效了其实没生效）。"""
    app, _ = await _client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        for key, bad in (("play_times", 9), ("volume", 999), ("speed", 99999)):
            r = await ac.post("/api/ops/notify-channels", json={
                "name": f"d152-{key}", "type": "aliyun_voice",
                "config_json": {**FULL_CFG, key: bad}})
            assert r.status_code == 400, f"{key}={bad} 未被拦：{r.text[:160]}"
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_s1_rule_accepts_aliyun_voice_channel():
    """S1 分级通知必须接受 aliyun_voice 作为"电话类渠道"（否则用户配了语音渠道却存不了规则）。"""
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.models.alert import AlertRule, NotificationChannel

    app, _ = await _client()
    name = "d152-s1-voice"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/ops/notify-channels", json={
            "name": name, "type": "aliyun_voice", "config_json": dict(FULL_CFG)})
        assert r.status_code == 200, r.text[:200]
        cid = r.json()["data"]["id"]

        r = await ac.post("/api/ops/alert-rules", json={
            "name": "d152-rule-s1", "scope": "metric", "metric": "cpu.percent",
            "expr": "> 90", "severity": 1, "enabled": True,
            "channels_by_level": {"1": [name], "2": [], "3": []}})
        assert r.status_code == 200, r.text[:300]

        async with SessionLocal() as db:
            await db.execute(delete(AlertRule).where(AlertRule.name == "d152-rule-s1"))
            await db.execute(delete(NotificationChannel).where(NotificationChannel.id == cid))
            await db.commit()
    app.dependency_overrides.clear()


# ───────────────────────── ⑤ 前端静态守卫 ─────────────────────────
def test_frontend_settings_has_aliyun_voice():
    """前端必须有阿里云语音的表单（否则用户在后端配好了也填不出来）。"""
    src = (ROOT / "static/js/pages/settings.js").read_text(encoding="utf-8")
    assert '"aliyun_voice"' in src
    assert "阿里云语音" in src
    for key in ("access_key_id", "access_key_secret", "tts_code", "called_show_number",
                "tts_param_key", "play_times", "volume", "speed"):
        assert key in src, f"前端缺字段：{key}"
    # 必填项标记 + 校验 + 掩码占位
    assert "AccessKey Secret" in src
    assert "留空 = 保持不变（已配置）" in src
    # 提示文案：接入点必须是语音服务的域（不是短信的）、权限写清 action
    assert "dyvmsapi.aliyuncs.com" in src
    assert "dysapi.aliyuncs.com" not in src, "前端还写着短信服务的域名"
    assert "dyvms:SingleCallByTts" in src
    assert "AliyunDysmsFullAccess" not in src, "AliyunDysmsFullAccess 是短信服务的策略名"
    # 模板变量名要讲清（= 模板正文里的变量名）
    assert "模板变量名" in src


def test_test_message_title_is_channel_aware():
    """D153：测试消息标题原先**写死**"[测试] 飞书通知渠道连通"。

    对阿里云语音渠道，这条标题会被**电话念出来**——用户会听到「飞书通知渠道连通」，
    渠道类型完全不对、还会让人以为拨错了产品。标题必须按渠道类型给。
    """
    from app.services.notify_format import describe_test

    t, text, card = describe_test("我的语音渠道", "", channel_type="aliyun_voice")
    assert "飞书" not in t, f"语音渠道的测试标题不该提飞书：{t}"
    assert "语音" in t, t
    t2, _, _ = describe_test("我的群", "", channel_type="feishu")
    assert "飞书" in t2, t2
    t3, _, _ = describe_test("未知渠道", "", channel_type="something_new")
    assert "飞书" not in t3, t3
    # 邮件/钉钉等也各自报自己的名字
    for ct, kw in (("dingtalk", "钉钉"), ("email", "邮件"), ("webhook", "Webhook")):
        tt, _, _ = describe_test("x", "", channel_type=ct)
        assert kw in tt, f"{ct} → {tt}"


@pytest.mark.asyncio
async def test_test_channel_endpoint_supports_aliyun_voice(monkeypatch):
    """「测试」按钮对 aliyun_voice 走的是自研直连（不是 apprise），且失败原因可读。

    用户问「可以测试链接吗」——答案是支持，但语义是**真打电话**：
    测试请求会真的发一通 TTS 呼叫。所以失败时的原因必须能照着改。
    """
    from sqlalchemy import delete
    from app.db.session import SessionLocal
    from app.models.alert import NotificationChannel

    app, _ = await _client()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/ops/notify-channels", json={
            "name": "d152-testbtn", "type": "aliyun_voice", "config_json": dict(FULL_CFG)})
        assert r.status_code == 200, r.text[:200]
        cid = r.json()["data"]["id"]

        # 打桩阿里云：返回鉴权失败
        from app.services import notify_transport as nt

        async def _fake(cfg, title, body):
            return False, "阿里云语音拒绝（InvalidAccessKeyId.NotFound）：AccessKey ID 不存在 —— 核对一下"

        monkeypatch.setattr(nt, "send_aliyun_voice", _fake)
        r = await ac.post(f"/api/ops/notify-channels/{cid}/test")
        assert r.status_code == 200, r.text[:200]
        d = r.json()["data"]
        assert d["ok"] is False
        assert "InvalidAccessKeyId.NotFound" in d["detail"], d["detail"]

        async with SessionLocal() as db:
            ch = await db.get(NotificationChannel, cid)
            assert ch.last_test_status == "failed"
            await db.execute(delete(NotificationChannel).where(NotificationChannel.id == cid))
            await db.commit()
    app.dependency_overrides.clear()
