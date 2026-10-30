"""通知传输层：apprise 适配（ADR-002）。

边界：本模块只负责"把一条消息送到渠道"，不涉及通知节奏/静默/台账（那些在 notify.py / mute.py）。
- apprise 是同步 requests 实现 → 一律经 asyncio.to_thread，不阻塞事件循环。
- 友好类型(config) → apprise URL 的映射；config_json.apprise_url 存在时直接透传（逃生口，可接 150+ 渠道）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import os
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, quote

import apprise
import httpx

# 各平台 webhook 的 token 位置：直接从用户填的整条 webhook_url 里抠 token，
# 用户不需要理解 apprise 的 URL 语法（映射层是自研价值所在，ADR-002 §3）。
_FEISHU_PREFIXES = ("https://open.feishu.cn/open-apis/bot/v2/hook/", "https://open.larksuite.com/open-apis/bot/v2/hook/")
_DINGTALK_HOST = "oapi.dingtalk.com"


def _feishu_token(webhook_url: str) -> str:
    u = webhook_url.strip()
    for p in _FEISHU_PREFIXES:
        if u.startswith(p):
            return u[len(p):].strip("/")
    return u.rsplit("/", 1)[-1].strip()  # 用户也可能只填了 token


def _dingtalk_token_secret(webhook_url: str) -> tuple[str, str]:
    q = parse_qs(urlparse(webhook_url).query)
    return (q.get("access_token", [""])[0], q.get("secret", [""])[0])


def to_apprise_url(ch_type: str, cfg: dict) -> str | None:
    """把平台的渠道配置翻成 apprise URL。返回 None 表示配置不完整。"""
    if ch_type == "aliyun_voice":
        # D152：阿里云语音**不走 apprise**（apprise 生态只有 twilio/telnyx/sinch/voipms
        # 四个语音插件，没有阿里云）。这里直接返回 None，让 send_channel 走
        # 自研的 RPC 签名直连实现（send_aliyun_voice）。
        # 必须早于下面的 apprise_url 透传分支——否则用户填了 apprise_url 会被
        # 误当成 apprise 渠道投递（而 apprise 解析不了 dysapi 的签名串）。
        return None

    if ch_type == "phone":
        # D140：电话渠道。apprise 生态支持语音呼叫（twilio / call / squirt 等 scheme），
        # 平台不自研电话网关。电话是 S1 的最后防线，配置错误必须在「测试」时暴露。
        #
        # D144：用户明确要求界面上能直接填手机号。**phone 分支必须放在 apprise_url
        # 透传之前**（透传会让 {phone} 占位原样发给网关）。配置支持三种写法：
        #   ① apprise_url 带 `{phone}` 占位 → 用 config.phone 替换（同一网关复用、只改被叫号）
        #   ② 填完整 apprise_url（含真实被叫号）→ 原样使用
        #   ③ 只填了手机号 → 用平台级默认语音网关 AIOPS_VOICE_GATEWAY_URL（同样支持 {phone}）
        # 只有手机号、又没有任何网关时返回 None：测试给出可读的"配置不完整"，
        # 而不是在 S1 告警现场静默失败。
        url = str(cfg.get("apprise_url", "")).strip()
        phone = str(cfg.get("phone", "")).strip()
        if url and "{phone}" in url:
            if not phone:
                return None      # 占位符没有手机号可填 → 配置不完整，不许把 {phone} 原样发给网关
            url = url.replace("{phone}", quote(phone, safe="+"))
        elif not url and phone:
            tpl = str(os.getenv("AIOPS_VOICE_GATEWAY_URL", "")).strip()
            if tpl and "{phone}" in tpl:
                url = tpl.replace("{phone}", quote(phone, safe="+"))
        return url or None

    if cfg.get("apprise_url"):
        return str(cfg["apprise_url"])

    if ch_type == "feishu":
        tok = _feishu_token(cfg.get("webhook_url", ""))
        return f"feishu://{tok}/" if tok else None

    if ch_type == "wecom":
        # D140：企业微信群机器人（用户口中的"微信群"——个人微信群无开放 API，
        # 标准做法是企业微信群机器人 webhook）。注意参数名是 key=（不是钉钉的 access_token=）。
        q = parse_qs(urlparse(cfg.get("webhook_url", "")).query)
        tok = q.get("key", [""])[0] or str(cfg.get("token", ""))
        return f"wecom://{tok}/" if tok else None

    if ch_type == "dingtalk":
        tok, sec = _dingtalk_token_secret(cfg.get("webhook_url", ""))
        if not tok:
            tok = str(cfg.get("token", ""))
        sec = sec or str(cfg.get("secret", ""))
        return f"dingtalk://{tok}/{sec}/" if tok else None

    if ch_type == "email":
        smtp = cfg.get("smtp", {}) or {}
        host, user = smtp.get("host", ""), smtp.get("user", "")
        pw, to = smtp.get("pass", ""), smtp.get("to", "")
        if not (host and user and to):
            return None
        port = str(smtp.get("port", 465))
        # apprise 按端口自动选 ssl/tls（465=implicit TLS、587=starttls），修掉自研版 465 用 starttls 的缺陷
        return f"mailto://{user}:{pw}@{host}:{port}?to={to}"

    if ch_type == "webhook":
        wu = cfg.get("webhook_url", "")
        if not wu:
            return None
        scheme = "jsons" if wu.startswith("https://") else "json"
        return f"{scheme}://" + wu.split("://", 1)[-1]

    return None


FRIENDLY_TYPES = ("feishu", "dingtalk", "wecom", "phone", "email", "webhook", "aliyun_voice")

# 危险 scheme 黑名单（夯实轮补）：apprise 的 file:// 会把通知内容**写到本地文件**——
# 配置里填 `file:///etc/cron.d/x` 这类路径等于拿通知当本地写文件通道。
# 通知渠道只应做网络投递，任何 URL 字段命中黑名单一律拒绝（内置类型也拦）。
_SCHEME_DENY = {"file", "scp", "ssh", "exec"}


def _url_scheme(url: str) -> str:
    return url.strip().split("://", 1)[0].lower() if "://" in url else ""


def validate_channel(ch_type: str, cfg: dict) -> str | None:
    """渠道类型校验。返回 None 表示可用，否则返回给人看的错误原因。

    为什么需要它：type 原先被数据库 ENUM 限死在 4 个值上，用户填 apprise 原生 scheme
    （如 `json`/`slack`）会直接撞 DB 报错 → HTTP 500（D8 实测）。这里改成显式校验：
    - 内置 4 类：放过协议细节（配置是否完整留给"测试渠道"给出可读反馈）
    - 其它 scheme：必须带 `apprise_url` 逃生口，且 apprise 能解析
    - 任何 URL 字段命中危险 scheme（file:// 等）一律拒绝——无论走哪个类型
    """
    t = (ch_type or "").strip().lower()
    if not t:
        return "通知类型不能为空"
    for field in ("apprise_url", "webhook_url"):
        u = str((cfg or {}).get(field) or "").strip()
        if u and _url_scheme(u) in _SCHEME_DENY:
            return f"渠道地址不允许使用 {u.split('://', 1)[0]}:// 协议（通知渠道只支持网络投递）"
    if t == "aliyun_voice":
        # D152：阿里云语音不走 apprise，配置完整性必须**在这里**挡（否则建出一个
        # 永远发不出去的死渠道，只能等告警响时才发现）。编辑时是合并后的配置
        # （空串=保留旧值），所以老凭据仍在 → 不会误判。
        missing = _aliyun_voice_missing(cfg or {})
        if missing:
            return f"阿里云语音渠道配置不完整，缺少：{'、'.join(missing)}"
        return None
    if t in FRIENDLY_TYPES:
        return None
    url = str((cfg or {}).get("apprise_url") or "").strip()
    if not url:
        return (f"不支持的通知类型「{t}」。内置类型：{'/'.join(FRIENDLY_TYPES)}；"
                f"其它渠道（slack/telegram/json/…）请把 type 设为 webhook 并填写 apprise_url")
    if not apprise.Apprise().add(url):
        return f"apprise 无法解析 apprise_url（scheme 不支持或格式错误）：{url.split('://', 1)[0]}://…"
    return None


# 单次投递的 socket 超时（秒）与整体墙钟上限（秒）
# 夯实轮补：apprise 底层是 requests，**默认无超时**——对端若"连接挂住"（防火墙 DROP、
# 服务假死）而非明确拒绝，发送会永久挂住；而 dispatch 在**告警消费链路**上，
# 一条挂住的投递会拖住整条通知流程。这里两层兜底：
#   ① URL 追加 `?timeout=`（apprise 原生支持）限制 socket 层
#   ② 外层 asyncio.wait_for 限制墙钟（防 apprise 内部任何路径绕过 socket 超时）
_NOTIFY_SOCKET_TIMEOUT_S = 10
_NOTIFY_WALL_TIMEOUT_S = 20


def _with_timeout(url: str, seconds: int = _NOTIFY_SOCKET_TIMEOUT_S) -> str:
    """给 apprise URL 追加 `timeout=` 参数（用户已显式设置则尊重原值）。"""
    if "timeout=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timeout={seconds}"


def _send_sync(url: str, title: str, body: str) -> tuple[bool, str]:
    ap = apprise.Apprise()
    if not ap.add(url):
        return False, f"apprise 无法解析渠道 URL: {url.split('://', 1)[0]}://…"
    ok = ap.notify(title=title, body=body)
    return bool(ok), ("ok" if ok else "apprise notify 失败（渠道返回非 2xx 或鉴权失败）")


async def send_via_apprise(ch_type: str, cfg: dict, title: str, body: str) -> tuple[bool, str]:
    """经 apprise 发送。同步库丢线程池 + **超时兜底**（避免挂住告警消费链路）。"""
    url = to_apprise_url(ch_type, cfg)
    if not url:
        return False, f"渠道配置不完整（type={ch_type}）"
    url = _with_timeout(url)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_send_sync, url, title, body),
            timeout=_NOTIFY_WALL_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return False, f"发送超时（>{_NOTIFY_WALL_TIMEOUT_S}s，对端无响应——已避免阻塞通知链路）"
    except Exception as e:  # noqa: BLE001 —— 通知失败不能影响告警主流程
        return False, f"{type(e).__name__}: {e}"


async def send_feishu_card(cfg: dict, card: dict) -> tuple[bool, str]:
    """直接发飞书交互卡片（apprise 的 feishu 插件只发文本，承载不了字段/颜色/按钮）。

    失败时由 send_channel 回退 apprise 文本；因此这里只负责"卡片这一跳"。
    """
    url = str((cfg or {}).get("webhook_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return False, "飞书渠道未配置可用的 webhook_url"
    payload: dict = {"msg_type": "interactive", "card": card}
    secret = str((cfg or {}).get("secret") or (cfg or {}).get("webhook_secret") or "")
    if secret:
        ts = str(int(time.time()))
        string_to_sign = f"{ts}\n{secret}"
        sign = base64.b64encode(
            hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        payload.update({"timestamp": ts, "sign": sign})
    try:
        async with httpx.AsyncClient(timeout=_NOTIFY_SOCKET_TIMEOUT_S) as client:
            r = await client.post(url, json=payload)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    if r.status_code != 200:
        return False, f"飞书 HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception:
        return False, "飞书响应不是 JSON"
    code = data.get("code", data.get("StatusCode", 0))
    if code not in (0, None):
        msg = data.get("msg") or data.get("StatusMessage") or f"code={code}"
        return False, f"飞书拒绝：{str(msg)[:180]}"
    return True, "feishu-card"


async def send_channel(ch_type: str, cfg: dict, title: str, body: str,
                       card: dict | None = None) -> tuple[bool, str]:
    """统一发送入口：飞书优先卡片，失败回退 apprise 文本；其它渠道保持 apprise。"""
    if ch_type == "feishu" and card and str((cfg or {}).get("webhook_url") or "").strip():
        ok, detail = await send_feishu_card(cfg, card)
        if ok:
            return True, detail
        ok2, d2 = await send_via_apprise(ch_type, cfg, title, body)
        if ok2:
            return True, f"卡片失败（{detail}），已回退文本"
        return False, f"卡片失败（{detail}）；文本回退失败（{d2}）"
    if ch_type == "aliyun_voice":
        # D152：阿里云语音自研直连（apprise 没有阿里云语音插件）
        return await send_aliyun_voice(cfg, title, body)
    return await send_via_apprise(ch_type, cfg, title, body)


# ───────────────────────── 阿里云语音（D152） ─────────────────────────
# 用户已在阿里云开通「语音服务」，要在平台里把告警**打电话播报**出去。
# 为什么不用 apprise：apprise 的语音插件只有 twilio / telnyx / sinch / voipms
# 四个国际网关，**没有阿里云**（阿里云的语音 API 是带签名的 RPC，不是 URL 订阅）。
# 所以这里自研一条最薄的直连实现：
#   Action=SingleCallByTts、Version=2017-05-25、Endpoint=dyvmsapi.aliyuncs.com
#   签名 = RPC v1.0（HMAC-SHA1）——标准库可算，不需要额外依赖（AGENTS.md 禁加依赖）。
#
# ★ 本节所有「接口事实」均以**官方 SDK 源码**为准（aliyun-python-sdk 的
#   dyvmsapi-20170525 / alibabacloud-tea-openapi），不是凭记忆写的：
#   - 接入点 dyvmsapi.aliyuncs.com（**不是** dysapi.aliyuncs.com——那是短信服务的域名前缀，
#     用错会打到短信服务去，报"产品不存在"）；
#   - 业务参数名 CalledNumber（被叫）/ TtsCode（模板编号）/ TtsParam（模板变量）/
#     CalledShowNumber（主叫显示号）/ PlayTimes / Speed / Volume / OutId
#     ——**不是** PhoneNumber / TemplateCode（那俩是短信服务的参数名）。
#   - 响应体：{"Code":"OK","Message":"OK","RequestId":…,"CallId":…}；
#     失败时 HTTP 4xx/5xx 且 body 同样带 Code/Message（SDK 也是从 body 里取错误码）。
#
# 关键实现细节（踩过的坑，写下来免得下次重犯）：
#   ① 参与签名的参数 = 公共参数 + 业务参数，**不含 Signature 本身**；
#   ② 排序按参数名字典序；每步都做 RFC3986 percent-encode（空格→%20，不是 +）；
#   ③ StringToSign = "GET&%2F&" + percent_encode(canonical_query)；
#      （HTTP 方法那一段必须与实际发出的方法一致：这里用 GET——老版官方 SDK 对
#        RPC 类接口就是发 GET；新版 SDK 发 POST，两者阿里云都收，但方法段不能混）
#   ④ Signature = base64(hmac_sha1(access_secret + "&", StringToSign))；
#   ⑤ **签名用和实际发出去完全相同的 percent-encode 串**，否则报 SignatureDoesNotMatch。
#      为此这里自己拼 query string（不交给 httpx 的 params 二次编码）。
#      （算法已与官方 SDK 的 Utils.get_rpcsignature 逐字节对拍，见回归测试）
_ALIYUN_VOICE_ENDPOINT = "https://dyvmsapi.aliyuncs.com"
_ALIYUN_VOICE_ACTION = "SingleCallByTts"
_ALIYUN_VOICE_VERSION = "2017-05-25"
# 语音内容长度上限：官方文档要求「模板中的变量总长 < 250 字符」。
# 留余量取 200——够念完一句告警，也不会顶到上限被拒。
_ALIYUN_VOICE_MAX_TEXT = 200
# TtsParam 的 key = **模板里那个变量的名字**（用户在阿里云控制台建模板时自己起名）。
# 默认 "content"（官方示例的常用名）；模板里变量不叫这个的话，用户可在渠道里改。
_ALIYUN_TTS_DEFAULT_KEY = "content"


def _aliyun_percent_encode(value: str) -> str:
    """RFC3986 percent-encode（阿里云签名专用）。

    Python 的 quote 默认 safe='/'，这里必须显式 safe=''，否则路径分隔符 /
    不会被编码，与服务端算出的签名对不上。
    """
    return quote(str(value), safe="")


def _aliyun_sign(access_secret: str, params: dict[str, str]) -> str:
    """阿里云 RPC v1.0 签名（HMAC-SHA1）。

    单独抽成纯函数是为了能单测锁死签名算法——签名错了返回的是
    SignatureDoesNotMatch，但排查时很难一眼看出是算法错还是参数错。

    防御：Signature 自身**不参与**签名（否则自相矛盾）——这里显式剔除，
    免得调用方误把已有签名一起传进来。
    """
    canonical = "&".join(
        f"{_aliyun_percent_encode(k)}={_aliyun_percent_encode(params[k])}"
        for k in sorted(params) if k != "Signature"
    )
    string_to_sign = f"GET&{_aliyun_percent_encode('/')}&{_aliyun_percent_encode(canonical)}"
    digest = hmac.new(
        (str(access_secret) + "&").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _aliyun_voice_text(title: str, body: str) -> str:
    """把告警标题+正文压成一句适合朗读的话（超长截断）。

    D164（用户验收要求）：电话里要听得出"哪台主机、哪块盘、使用率多少、超过阈值多少"，
    结尾要有行动指令。三件事：
      ① 剥 markdown——`**` 会被 TTS 念成"星星星"，粗体标记必须先去掉；
      ② 触发（非恢复）在结尾加"，请尽快处理"——用户原话："请立刻处理之类的"；
      ③ 超长截断保留在末尾截（阿里云模板变量总长 <250 字符，正文信息前置）。
    """
    text = f"{title}。{body}".strip()
    # markdown 剥除：粗体/斜体/行内代码/链接只留文字（TTS 没有排版）
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # 换行/多空白在 TTS 里会被念成"逗号"，压成空格更自然
    text = " ".join(text.split())
    if "已恢复" not in title and "已恢复" not in text[:30]:
        if "请尽快处理" not in text and "请立刻处理" not in text:
            text = f"{text}，请尽快处理。"
    if len(text) > _ALIYUN_VOICE_MAX_TEXT:
        text = text[:_ALIYUN_VOICE_MAX_TEXT]
    return text


def _aliyun_voice_missing(cfg: dict) -> list[str]:
    """返回配置里缺失的必填项（空列表 = 完整）。"""
    c = cfg or {}
    need = {
        "access_key_id": "AccessKey ID",
        "access_key_secret": "AccessKey Secret",
        "tts_code": "语音模板 Code",
        "phone": "被叫手机号",
    }
    return [label for key, label in need.items() if not str(c.get(key) or "").strip()]


# 阿里云语音的常见错误码 → 人话解释 + 下一步该干什么。
# 为什么要有这张表：阿里云返回的 Message 是**英文**的（"Specified access key is not found."），
# 光把它丢给用户等于让用户自己翻译 + 自己猜。而 Code 是**可检索的机器可读键**
# （能拿去搜阿里云《API 错误码》文档），两者一起给才对。
# 表里只放"用户自己就能改"的错；权限/额度/欠费这类要给明确的下一步，而不是让用户猜。
_ALIYUN_VOICE_ERRORS = {
    "InvalidAccessKeyId.NotFound": (
        "AccessKey ID 不存在",
        "核对渠道里的 AccessKey ID 是否填错、是否已被删除（建议在 RAM 里新建一个只给语音服务权限的 RAM 用户）"),
    "InvalidAccessKeyId": (
        "AccessKey ID 无效",
        "核对 AccessKey ID 是否与 AccessKey Secret 配对（同一对 ID/Secret 必须一起创建）"),
    "SignatureDoesNotMatch": (
        "签名校验不通过",
        "多为 AccessKey Secret 填错；请重新复制一次 Secret（只显示一次，粘错很常见）"),
    "Forbidden.AccessKeyDisabled": (
        "AccessKey 已被禁用",
        "到 RAM「安全管理 → AccessKey」里启用或重新创建"),
    "Forbidden.RAM": (
        "RAM 权限不足",
        "给该 AccessKey 所属的 RAM 用户授予语音服务权限：系统策略「语音服务」，或自定义策略放行 dyvms:SingleCallByTts"),
    "isv.BUSINESS_LIMIT_CONTROL": (
        "呼叫频率/额度超限",
        "阿里云侧对语音通知有频率限制（同号码 1 分钟 1 次、1 小时 5 次、24 小时 20 次）；请降低告警重复通知的频率"),
    "isv.OUT_OF_SERVICE": (
        "语音服务停机",
        "阿里云语音服务当前不可用（可查阿里云语音服务公告），稍后重试"),
    "isv.MOBILE_NUMBER_ILLEGAL": (
        "被叫号码不合法",
        "核对被叫号码：国内 13800138000，国际 +8613800138000（不要带空格或横线）"),
    "isv.TEMPLATE_ILLEGAL": (
        "语音模板不存在或未审核通过",
        "在阿里云「语音服务 → 通知模板」确认模板 Code 填对、且模板状态是已审核通过"),
    "isv.TEMPLATE_CODE_ILLEGAL": (
        "语音模板 Code 不合法",
        "核对模板 Code（形如 TTS_123456789）；注意它和短信的模板 ID 不是一回事"),
    "isv.TTS_PARAM_ILLEGAL": (
        "模板变量与模板正文对不上",
        "模板正文里要有与「模板变量名」一致的变量（默认 content）；只念模板固定文案就是这里没配对"),
    "isv.SMS_TEMPLATE_ILLEGAL": (
        "模板类型不对",
        "「单条语音通知」要用**语音通知**模板，不能用语音验证码或短信模板"),
    "isv.OUT_OF_WORK": (
        "服务未开通",
        "该阿里云账号还没开通「语音服务」产品，请先在控制台开通"),
    "isv.PRODUCT_UNSUBSCRIBE": (
        "语音服务未开通或已退订",
        "在阿里云控制台开通「语音服务」并完成企业认证/资质审核"),
}


def _aliyun_voice_error(code: str, data: dict) -> str:
    """把阿里云的错误响应翻成一句能照着做的话。

    格式：`阿里云语音拒绝（<Code>）：<中文解释> —— <下一步>（原文：<Message>）`
    Code 必须带上：它是用户唯一能拿去搜官方错误码文档的东西。
    """
    msg = str(data.get("Message") or "").strip()
    extra = str(data.get("Detail") or data.get("Description") or "").strip()
    zh, hint = _ALIYUN_VOICE_ERRORS.get(code, (None, None))
    out = f"阿里云语音拒绝（{code}）"
    if zh:
        out += f"：{zh}"
        if hint:
            out += f" —— {hint}"
    if msg:
        out += f"（原文：{msg[:140]}）"
    if extra:
        out += f"（详情：{extra[:100]}）"
    return out[:460]


def _split_phones(raw: str) -> list[str]:
    """D165：被叫号码支持**逗号/分号/中文逗号分隔多个**——"13511112222,13533334444"。

    同一渠道一次告警可以打到值班组的多个人；去重、去空，保持填写顺序。
    （阿里云流控按被叫号码分别计，多号互不挤兑；同一号码填两次没有意义，去重。）
    """
    import re as _re
    out = []
    for piece in _re.split(r"[,，;；\s]+", str(raw or "")):
        piece = piece.strip()
        if piece and piece not in out:
            out.append(piece)
    return out


async def send_aliyun_voice(cfg: dict, title: str, body: str) -> tuple[bool, str]:
    """直连阿里云语音服务发 TTS 电话（D165：支持**多个被叫号码**，逐个拨打）。

    返回 (ok, detail)：detail 是给人看的失败原因（会写进通知台账）。
    多号时聚合结果——"2/3 通成功：…；135xxx 失败：原因"；只要有一通成功即 ok=True
    （告警的目的是叫到人，第一个打通就算尽到通知义务，失败明细仍然完整呈现）。
    """
    c = cfg or {}
    missing = _aliyun_voice_missing(c)
    if missing:
        return False, f"阿里云语音渠道配置不完整，缺少：{'、'.join(missing)}"

    phones = _split_phones(str(c.get("phone") or ""))
    if not phones:
        return False, "阿里云语音渠道未配置被叫号码"
    if len(phones) == 1:
        return await _send_aliyun_voice_one(c, phones[0], title, body)
    results = []
    ok_count = 0
    for ph in phones:
        ok, detail = await _send_aliyun_voice_one(c, ph, title, body)
        results.append((ph, ok, detail))
        if ok:
            ok_count += 1
    ok_all = ", ".join(ph for ph, ok, _ in results if ok)
    bad = "；".join(f"{ph}：{detail}" for ph, ok, _ in results if not ok)
    n = len(phones)
    if ok_count == n:
        return True, f"aliyun-voice ok（{n}/{n} 通成功：{ok_all}）"
    if ok_count:
        return True, f"aliyun-voice 部分成功（{ok_count}/{n} 通：{ok_all}）；失败——{bad[:240]}"
    return False, f"aliyun-voice 全部失败（0/{n}）——{bad[:300]}"


async def _send_aliyun_voice_one(c: dict, phone: str, title: str, body: str) -> tuple[bool, str]:
    """拨给**单个**号码（原 send_aliyun_voice 主体，D165 拆出以便多号循环）。"""
    text = _aliyun_voice_text(title, body)
    tts_key = str(c.get("tts_param_key") or "").strip() or _ALIYUN_TTS_DEFAULT_KEY

    params: dict[str, str] = {
        "AccessKeyId": str(c.get("access_key_id") or "").strip(),
        "Action": _ALIYUN_VOICE_ACTION,
        "Format": "JSON",
        "CalledNumber": phone,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "TtsParam": json.dumps({tts_key: text}, ensure_ascii=False),
        "TtsCode": str(c.get("tts_code") or "").strip(),
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": _ALIYUN_VOICE_VERSION,
    }
    # 可选参数（官方 SDK 的 SingleCallByTtsRequest 里有，这里只暴露告警场景常用的几个）：
    #   CalledShowNumber 主叫显示号（通用模式下可省；专用模式下必须填已备案的真实号）
    #   PlayTimes       播放次数 1~3（默认 3）——夜里没人接时多播几遍
    #   Speed           语速 -500~500（0 为默认）　Volume 音量 0~100（默认 100）
    #   OutId           自定义流水号 1~15 字节——会在**回执消息**里带回，
    #                   用于在阿里云控制台按来源筛这批呼叫（官方文档要求 1~15 字节）
    show_number = str(c.get("called_show_number") or "").strip()
    if show_number:
        params["CalledShowNumber"] = show_number
    out_id = str(c.get("out_id") or "").strip()
    if out_id:
        # 官方要求 1~15 字节；超长阿里云会拒，截断反而会静默丢信息 → 直接不发（前端已校验）
        if len(out_id.encode("utf-8")) <= 15:
            params["OutId"] = out_id
    for key, lo, hi in (("play_times", 1, 3), ("speed", -500, 500), ("volume", 0, 100)):
        raw = c.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if lo <= val <= hi:
            params[{"play_times": "PlayTimes", "speed": "Speed", "volume": "Volume"}[key]] = str(val)

    params["Signature"] = _aliyun_sign(str(c.get("access_key_secret") or ""), params)
    # 自己拼 query：签名用的就是这串 percent-encode 结果，必须与实际请求完全一致
    query = "&".join(
        f"{_aliyun_percent_encode(k)}={_aliyun_percent_encode(params[k])}"
        for k in sorted(params)
    )
    url = f"{_ALIYUN_VOICE_ENDPOINT}?{query}"

    try:
        async with httpx.AsyncClient(timeout=_NOTIFY_SOCKET_TIMEOUT_S) as client:
            r = await asyncio.wait_for(
                client.get(url), timeout=_NOTIFY_WALL_TIMEOUT_S)
    except asyncio.TimeoutError:
        return False, f"阿里云语音发送超时（>{_NOTIFY_WALL_TIMEOUT_S}s）"
    except Exception as e:  # noqa: BLE001 —— 通知失败不能影响告警主流程
        return False, f"{type(e).__name__}: {e}"

    # 失败时 HTTP 是 4xx/5xx，但 **body 里同样有 Code/Message**（官方 SDK 也是从 body 取错误码），
    # 所以先解 body 再看状态码——否则用户只会看到"HTTP 403"这种没法排查的话。
    try:
        data = r.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    code = str(data.get("Code") or "")
    if code and code != "OK":
        return False, _aliyun_voice_error(code, data)
    if r.status_code != 200:
        return False, f"阿里云语音 HTTP {r.status_code}"
    if not code:
        # 没有 Code 字段 = 响应不是预期的 JSON（网关错误页等）
        return False, "阿里云语音响应异常（未返回 Code 字段）"
    call_id = str(data.get("CallId") or "").strip()
    suffix = f"，CallId={call_id}" if call_id else ""
    return True, f"aliyun-voice ok（呼叫 {phone}{suffix}）"

