"""D98 守卫：LLM 报错必须转成用户能懂的人话（原始 JSON/英文堆栈只进日志）。

背景（额度耗尽现场实测）：AI 助手曾把 "RateLimitError: Error code: 429 - {...}"
整段抛给用户——技术黑话 + 英文 + 原始 JSON，用户无法据此行动。
本文件锁定：网关把服务商异常映射为友好中文；诊断链路的失败原因同样可读。
"""
import asyncio
from pathlib import Path

from fastapi import HTTPException
from openai import APIConnectionError, APIStatusError, AuthenticationError, RateLimitError

from app.services import llm_gateway

ROOT = Path(__file__).resolve().parents[2]

HTTP_429_BODY = (
    '{"type": "error", "error": {"type": "rate_limit_error", '
    '"message": "已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。 (2056)", '
    '"http_code": "429"}}'
)


class _FakeClient:
    """让 client.chat.completions.create 抛指定异常。"""

    def __init__(self, err: Exception):
        self._err = err
        self.chat = self
        self.completions = self

    def create(self, **kw):
        raise self._err


def _err(cls, body: str, code: int | None = None) -> Exception:
    import httpx

    req = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    if code is not None:
        resp = httpx.Response(status_code=code, request=req, text=body)
        try:
            return cls(body, response=resp, body=body)
        except TypeError:
            return cls(body)
    return cls(_conn_msg())


def _call_once(monkeypatch, err: Exception) -> HTTPException:
    """跑一次 call_llm（重试 2 次都失败）→ 断言抛出的 HTTPException。"""

    class _Cfg:
        model = "test-model"
        fast_model = ""
        base_url = "https://llm.example"
        api_key_encrypted = ""          # _api_key_of: 无 _plain_key 时走解密，空串回退 sk-none
        extra_config: dict = {}

        @property
        def _plain_key(self):
            return "test-key"

    async def _pick(db, model_role="main"):
        return _Cfg()

    async def _fake_async_client(base_url=None, api_key=None):
        return _FakeClient(err)

    import app.services.llm_gateway as gw

    monkeypatch.setattr(gw, "_pick_config_for_test", _pick, raising=False)
    # call_llm 内部直接构造 client：monkeypatch AsyncOpenAI 构造入口
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: _FakeClient(err))
    monkeypatch.setattr(gw, "pick_config", _pick)
    # D184：本地/CI 库里可能存在真实降级通道（429 会走降级分支，文案混入通道名）——
    # 本测试只验证主路径的人话映射，降级一律视为"未配置"。
    async def _no_fallback(db):
        raise HTTPException(503, "LLM_FALLBACK_UNCONFIGURED（测试桩）")
    monkeypatch.setattr(gw, "pick_fallback_config", _no_fallback)

    async def _run():
        async with __import__('app.db.session', fromlist=['SessionLocal']).SessionLocal() as db:
            await gw.call_llm(db, "selftest", [{"role": "user", "content": "hi"}], model_role="main")

    try:
        asyncio.run(_run())
    except HTTPException as he:
        return he
    raise AssertionError("call_llm 应当抛 HTTPException")


def test_rate_limit_maps_to_friendly_quota_message(monkeypatch):
    """429 限流 → 用户看到"额度用完+怎么办"，且不含原始 JSON/英文堆栈。

    D184 语义更新：429 会先走降级分支（D163-3）。未配降级通道时的人话即
    LLM_FALLBACK_UNCONFIGURED（自带"额度用完+怎么配降级"指引）；配上降级且也失败时，
    文案讲清两条通道各自的失败原因。两种形态都必须无原始报错。
    """
    he = _call_once(monkeypatch, _err(RateLimitError, HTTP_429_BODY, 429))
    assert he.status_code == 503
    msg = he.detail
    assert "额度" in msg or "LLM_FALLBACK_UNCONFIGURED" in msg, f"应说明额度用完/未配降级：{msg}"
    assert "RateLimitError" not in msg and "{" not in msg, (
        f"用户可见文案不得包含原始报错细节：{msg}")


def test_auth_error_maps_to_key_hint(monkeypatch):
    he = _call_once(monkeypatch, _err(AuthenticationError, "invalid api key", 401))
    assert "密钥" in he.detail, he.detail
    assert "invalid api key" not in he.detail


def test_connection_error_maps_to_retry_hint(monkeypatch):
    he = _call_once(monkeypatch, _err(APIConnectionError, _req=None) if False else _conn_err())
    assert ("超时" in he.detail or "连接" in he.detail or "稍后重试" in he.detail), he.detail


def _conn_err():
    import httpx

    req = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    exc = APIConnectionError(message="connection failed", request=req)
    return exc  # message 参数在父类


def _conn_msg():
    return "connection failed"


def test_gateway_source_has_mapping() -> None:
    """源码守卫：网关必须包含人话映射（额度/密钥/超时），不得回退成裸异常拼接。"""
    src = (ROOT / "backend/app/services/llm_gateway.py").read_text(encoding="utf-8")
    assert "额度已用完" in src and "密钥无效" in src and "连接超时" in src
    assert "log.warning" in src, "原始报错应进日志（不给用户看）"
