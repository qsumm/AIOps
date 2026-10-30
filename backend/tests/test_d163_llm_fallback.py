"""D163-3：主模型额度耗尽时自动降级到快模型（服务商的免费额度按模型分别计算）。

**现场**（公网实测）：主模型 qwen3.8-max 返回 403 "Free quota exhausted"，
而同账户的快模型 qwen3.8-flash **额度独立、仍可用**（实测快模型正常返回）。
原实现此时让 AI 助手**整个不可用**——明明还有能用的模型。

降级边界（重要）：只在「服务商侧额度/限流」时降级。
  模型不存在（404 model_not_found）、密钥错（401）、参数错（400）是**配置问题**，
  降级到快模型同样会失败 —— 不该降级，否则会掩盖真实错误。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.llm_gateway import _is_quota_or_rate_limit

# openai 库的异常类名（openai.PermissionDeniedError 等）——
# 平台按 `type(e).__name__` 分类，这里用同名类模拟（**不要**直接用 openai 的，
# 免得测试依赖具体 SDK 版本）。
class PermissionDeniedError(Exception):
    """模拟 openai.PermissionDeniedError。"""


class RateLimitError(Exception):
    """模拟 openai.RateLimitError。"""


def test_is_quota_or_rate_limit_classification():
    """只有额度/限流类错误才可降级；配置类错误不降级。"""
    # 可降级
    assert _is_quota_or_rate_limit(RuntimeError("Error code: 429 - rate limited"))
    assert _is_quota_or_rate_limit(RuntimeError("Error code: 403 - Free quota exhausted."))
    assert _is_quota_or_rate_limit(RuntimeError("PermissionDeniedError: insufficient_quota"))
    assert _is_quota_or_rate_limit(RuntimeError("You exceeded your current quota"))

    # 不可降级（配置问题，快模型同样会失败）
    assert not _is_quota_or_rate_limit(RuntimeError("Error code: 404 - Model not exist."))
    assert not _is_quota_or_rate_limit(RuntimeError("Error code: 401 - Incorrect API key"))
    assert not _is_quota_or_rate_limit(RuntimeError("Error code: 400 - InvalidParameter"))


def test_rate_limit_error_name_detected():
    err = RateLimitError("Error code: 429 - slow down")
    assert _is_quota_or_rate_limit(err)


@pytest.mark.asyncio
async def test_main_quota_exhausted_uses_configured_fallback_channel(monkeypatch):
    """主模型 403 → 改用**用户显式指定的降级通道**（独立配置：不同 base_url/key/模型）。

    D163-3（用户明确纠正）：降级目标**必须由用户指定**。
    平台不得擅自改用同一配置的 fast_model —— 用户配 fast_model 是为了"轻量任务用快模型"，
    **不等于**允许它在主模型额度耗尽时兜底（用户原话："万一 qwen3.8-flash 用户不允许用呢"）。
    """
    from app.services import llm_gateway

    calls = []

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15

    class _Msg:
        content = "降级通道的回答"
        tool_calls = None
        reasoning_content = ""

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        usage = _Usage()
        choices = [_Choice()]

    class _Completions:
        async def create(self, model=None, **kw):
            calls.append(model)
            if model == "main-model":
                raise PermissionDeniedError("Error code: 403 - Free quota exhausted.")
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()
        def __init__(self, base_url=None, api_key=None):
            calls.append(f"client:{base_url}")

    class _Cfg:
        def __init__(self, name, model, base_url, fast_model="", fb=False):
            self.name = name
            self.model = model
            self.base_url = base_url
            self.fast_model = fast_model
            self.fallback_enabled = fb
            self.api_key_encrypted = "k"
            self.enabled = True
            self.extra_config = {}

    MAIN = _Cfg("主通道", "main-model", "https://main/v1", fast_model="fast-model")
    FALLBACK = _Cfg("降级通道", "fallback-model", "https://fallback/v1", fb=True)

    async def _pick(db, role="main"):
        return MAIN

    async def _pick_fb(db):
        return FALLBACK

    async def _enf(db, role=""):
        return None

    monkeypatch.setattr(llm_gateway, "pick_config", _pick)
    monkeypatch.setattr(llm_gateway, "pick_fallback_config", _pick_fb)
    monkeypatch.setattr(llm_gateway, "_api_key_of", lambda c: "k")
    monkeypatch.setattr(llm_gateway, "enforce_quota", _enf)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kw: _Client(**kw))

    class _Result:
        def scalars(self):
            return self

        def first(self):
            return None      # 没有配置降级通道

    class _DB:
        def add(self, *a):
            pass

        async def commit(self):
            pass

        async def execute(self, *a, **kw):
            return _Result()

    r = await llm_gateway.call_llm(_DB(), "chat", [{"role": "user", "content": "hi"}],
                                   model_role="main", quota_role="admin")
    assert r.content == "降级通道的回答"
    assert r.model == "fallback-model", r.model
    # **绝不能**用到主配置里的 fast_model
    assert "fast-model" not in calls, f"降级不得擅自用 fast_model，实际调用 {calls}"
    assert "fallback-model" in calls, calls
    # 降级通道用自己的 base_url
    assert any(c == "client:https://fallback/v1" for c in calls), calls


@pytest.mark.asyncio
async def test_no_fallback_channel_configured_reports_clearly(monkeypatch):
    """没配降级通道 → 如实报错（而不是擅自拿 fast_model 顶上）。"""
    from app.services import llm_gateway

    class _Completions:
        async def create(self, model=None, **kw):
            raise PermissionDeniedError("Error code: 403 - Free quota exhausted.")

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    class _Cfg:
        name = "主通道"
        model = "main-model"
        base_url = "https://main/v1"
        fast_model = "fast-model"
        fallback_enabled = False
        api_key_encrypted = "k"
        enabled = True
        extra_config = {}

    async def _pick(db, role="main"):
        return _Cfg()

    async def _enf(db, role=""):
        return None

    monkeypatch.setattr(llm_gateway, "pick_config", _pick)
    monkeypatch.setattr(llm_gateway, "_api_key_of", lambda c: "k")
    monkeypatch.setattr(llm_gateway, "enforce_quota", _enf)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kw: _Client())

    class _Result:
        def scalars(self):
            return self

        def first(self):
            return None      # 没有配置降级通道

    class _DB:
        def add(self, *a):
            pass

        async def commit(self):
            pass

        async def execute(self, *a, **kw):
            return _Result()

    with pytest.raises(HTTPException) as ei:
        await llm_gateway.call_llm(_DB(), "chat", [{"role": "user", "content": "hi"}],
                                   model_role="main", quota_role="admin")
    assert ei.value.status_code == 503
    detail = str(ei.value.detail)
    assert "降级通道" in detail, detail
    assert "不会擅自" in detail or "不会" in detail, "提示要说清平台不会擅自替用户选模型"


@pytest.mark.asyncio
async def test_no_fallback_for_model_not_found(monkeypatch):
    """模型不存在（404）→ **不降级**，直接报错（配置问题，快模型同样会 404）。"""
    from app.services import llm_gateway

    calls = []

    class _Client:
        class chat:
            class completions:
                @staticmethod
                async def create(model=None, **kw):
                    calls.append(model)
                    raise RuntimeError("Error code: 404 - Model not exist.")

    class _Cfg:
        model = "main-model"
        fast_model = "fast-model"
        base_url = "https://x/v1"
        api_key_encrypted = "k"
        enabled = True
        extra_config = {}

    async def _pick(db, role="main"):
        return _Cfg()

    async def _enf(db, role=""):
        return None

    monkeypatch.setattr(llm_gateway, "pick_config", _pick)
    monkeypatch.setattr(llm_gateway, "_api_key_of", lambda c: "k")
    monkeypatch.setattr(llm_gateway, "enforce_quota", _enf)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kw: _Client())

    class _Result:
        def scalars(self):
            return self

        def first(self):
            return None      # 没有配置降级通道

    class _DB:
        def add(self, *a):
            pass

        async def commit(self):
            pass

        async def execute(self, *a, **kw):
            return _Result()

    with pytest.raises(HTTPException) as ei:
        await llm_gateway.call_llm(_DB(), "chat", [{"role": "user", "content": "hi"}],
                                   model_role="main", quota_role="admin")
    assert "fast-model" not in calls, f"不该降级，实际调用序列 {calls}"
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_fast_model_failing_keeps_original_error(monkeypatch):
    """快模型也失败 → 报**原始错误**（主模型的额度耗尽），不掩盖。"""
    from app.services import llm_gateway

    class _Client:
        class chat:
            class completions:
                @staticmethod
                async def create(model=None, **kw):
                    raise PermissionDeniedError("Error code: 403 - Free quota exhausted.")

    class _Cfg:
        model = "main-model"
        fast_model = "fast-model"
        base_url = "https://x/v1"
        api_key_encrypted = "k"
        enabled = True
        extra_config = {}

    async def _pick(db, role="main"):
        return _Cfg()

    async def _enf(db, role=""):
        return None

    monkeypatch.setattr(llm_gateway, "pick_config", _pick)
    monkeypatch.setattr(llm_gateway, "_api_key_of", lambda c: "k")
    monkeypatch.setattr(llm_gateway, "enforce_quota", _enf)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kw: _Client())

    class _Result:
        def scalars(self):
            return self

        def first(self):
            return None      # 没有配置降级通道

    class _DB:
        def add(self, *a):
            pass

        async def commit(self):
            pass

        async def execute(self, *a, **kw):
            return _Result()

    with pytest.raises(HTTPException) as ei:
        await llm_gateway.call_llm(_DB(), "chat", [{"role": "user", "content": "hi"}],
                                   model_role="main", quota_role="admin")
    msg = str(ei.value.detail)
    assert "额度" in msg, msg


@pytest.mark.asyncio
async def test_fallback_does_not_write_illegal_purpose(monkeypatch):
    """D163-3：降级**不得**往 llm_usage.purpose 写 ENUM 之外的值。

    `purpose` 是 ENUM(rca/chat/summary/inspect/fixcheck/embed/suggest)——
    写 "chat.fallback" 会 DataError（1265 Data truncated）→ 整个请求 500
    （公网实测：降级逻辑本身是对的，但被这行写入打挂，表现为"助手完全不可用"）。
    """
    from app.services import llm_gateway

    added = []

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15

    class _Msg:
        content = "降级后的回答"
        tool_calls = None
        reasoning_content = ""

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        usage = _Usage()
        choices = [_Choice()]

    class _Client:
        class chat:
            class completions:
                @staticmethod
                async def create(model=None, **kw):
                    if model == "main-model":
                        raise PermissionDeniedError("Error code: 403 - Free quota exhausted.")
                    return _Resp()

    class _Cfg:
        name = "主通道"
        model = "main-model"
        fast_model = "fast-model"
        base_url = "https://x/v1"
        api_key_encrypted = "k"
        enabled = True
        extra_config = {}

    class _FbCfg(_Cfg):
        name = "降级通道"
        model = "fallback-model"
        base_url = "https://fallback/v1"

    async def _pick(db, role="main"):
        return _Cfg()

    async def _enf(db, role=""):
        return None

    async def _pick_fb(db):
        return _FbCfg()

    monkeypatch.setattr(llm_gateway, "pick_config", _pick)
    monkeypatch.setattr(llm_gateway, "pick_fallback_config", _pick_fb)
    monkeypatch.setattr(llm_gateway, "_api_key_of", lambda c: "k")
    monkeypatch.setattr(llm_gateway, "enforce_quota", _enf)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kw: _Client())

    class _DB:
        def add(self, obj):
            added.append(obj)

        async def commit(self):
            pass

    r = await llm_gateway.call_llm(_DB(), "chat", [{"role": "user", "content": "hi"}],
                                   model_role="main", quota_role="admin")
    assert r.content == "降级后的回答"
    # purpose 必须是合法枚举值
    from app.models.llm import LLMUsage
    legal = LLMUsage.__table__.c.purpose.type.enums
    for obj in added:
        assert obj.purpose in legal, f"purpose={obj.purpose!r} 不在枚举 {legal} 内（会 DataError 500）"


@pytest.mark.asyncio
async def test_fallback_channel_prefers_its_fast_model(monkeypatch):
    """降级通道若配了 fast_model → 用它（那才是该配置里备着的另一个模型）。

    实测：降级通道走 model 会打回主模型同一个额度耗尽的模型 —— 白跑一次。
    """
    from app.services import llm_gateway

    calls = []

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15

    class _Msg:
        content = "降级回答"
        tool_calls = None
        reasoning_content = ""

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        usage = _Usage()
        choices = [_Choice()]

    class _Completions:
        async def create(self, model=None, **kw):
            calls.append(model)
            if model == "main-model":
                raise PermissionDeniedError("Error code: 403 - Free quota exhausted.")
            return _Resp()

    class _Client:
        class chat:
            completions = _Completions()

    class _Cfg:
        name = "主通道"
        model = "main-model"
        fast_model = "fast-model"
        base_url = "https://x/v1"
        api_key_encrypted = "k"
        enabled = True
        extra_config = {}

    class _FbCfg(_Cfg):
        name = "降级通道"
        model = "fb-main-model"
        fast_model = "fb-fast-model"
        base_url = "https://fb/v1"

    async def _pick(db, role="main"):
        return _Cfg()

    async def _pick_fb(db):
        return _FbCfg()

    async def _enf(db, role=""):
        return None

    monkeypatch.setattr(llm_gateway, "pick_config", _pick)
    monkeypatch.setattr(llm_gateway, "pick_fallback_config", _pick_fb)
    monkeypatch.setattr(llm_gateway, "_api_key_of", lambda c: "k")
    monkeypatch.setattr(llm_gateway, "enforce_quota", _enf)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kw: _Client())

    class _DB:
        def add(self, *a):
            pass

        async def commit(self):
            pass

    r = await llm_gateway.call_llm(_DB(), "chat", [{"role": "user", "content": "hi"}],
                                   model_role="main", quota_role="admin")
    assert r.model == "fb-fast-model", f"降级应用 fast_model，实际 {r.model}"
    assert "fb-main-model" not in calls, calls


@pytest.mark.asyncio
async def test_fallback_error_message_has_no_raw_json(monkeypatch):
    """D98 纪律：用户可见文案不得含服务商原始 JSON/英文堆栈。

    实测：降级失败时的提示里带了 "PermissionDeniedError: Error code: 403 - {...}"，
    用户看到一坨原始报错 —— 违反 D98（原始堆栈只进日志）。
    """
    from app.services import llm_gateway

    class _Completions:
        async def create(self, model=None, **kw):
            raise PermissionDeniedError("Error code: 403 - Free quota exhausted. {json}")

    class _Client:
        class chat:
            completions = _Completions()

    class _Cfg:
        name = "主通道"
        model = "main-model"
        fast_model = ""
        base_url = "https://main/v1"
        api_key_encrypted = "k"
        enabled = True
        extra_config = {}

    class _FbCfg(_Cfg):
        name = "降级通道"
        model = "fb-model"
        base_url = "https://fb/v1"

    async def _pick(db, role="main"):
        return _Cfg()

    async def _pick_fb(db):
        return _FbCfg()

    async def _enf(db, role=""):
        return None

    monkeypatch.setattr(llm_gateway, "pick_config", _pick)
    monkeypatch.setattr(llm_gateway, "pick_fallback_config", _pick_fb)
    monkeypatch.setattr(llm_gateway, "_api_key_of", lambda c: "k")
    monkeypatch.setattr(llm_gateway, "enforce_quota", _enf)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kw: _Client())

    class _DB:
        def add(self, *a):
            pass

        async def commit(self):
            pass

    with pytest.raises(HTTPException) as ei:
        await llm_gateway.call_llm(_DB(), "chat", [{"role": "user", "content": "hi"}],
                                   model_role="main", quota_role="admin")
    msg = str(ei.value.detail)
    assert "降级通道" in msg, msg
    for bad in ("PermissionDeniedError", "Error code:", "{", "json"):
        assert bad not in msg, f"用户可见文案不得含原始报错细节（{bad}）：{msg}"
