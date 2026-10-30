"""D163-2：embedding 模型名不能硬编码某服务商；服务商额度耗尽要给对的提示。

**现场**（公网实测）：案例库的向量召回**一直不可用**，日志只报
  `EMBED_UNAVAILABLE：响应形状不可识别 {"error":{"message":"Model not exist."}}`
——看起来像"响应形状不对"，实际是**模型名配错**。

根因：`(cfg.extra_config or {}).get("embedding_model") or "embo-01"`
  - `embo-01` 是 **MiniMax** 的 embedding 模型名，被**硬编码**成兜底值；
  - 公网配的是**阿里云 MaaS**（base_url 含 maas.aliyuncs.com），那边没有 embo-01
    → 每次调用 404 model_not_found；
  - 只因"降级为文本匹配"兜底才没炸成报错 —— **向量召回（案例库语义检索）实际一直是关的**。

修：① 兜底值改为**按 base_url 推断**服务商的默认模型；② 模型不存在时**说清是模型名的问题**；
    ③ 服务商账户额度耗尽（403）单独给提示（重试无用，要充值）。
"""
from __future__ import annotations

import pytest

from app.services.llm_gateway import (default_embedding_model,
                                       EMBED_MODEL_FALLBACK)


def test_default_embedding_model_by_provider():
    """按 base_url 推断服务商的默认 embedding 模型。"""
    # 阿里云取 v1 而非 v3：v3 默认输出 1024 维，写不进 VECTOR(1536)（实测 dimensions=1536 被拒）
    assert default_embedding_model(
        "https://llm-xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1") == "text-embedding-v1"
    assert default_embedding_model(
        "https://dashscope.aliyuncs.com/compatible-mode/v1") == "text-embedding-v1"
    assert default_embedding_model("https://api.openai.com/v1") == "text-embedding-3-small"
    assert default_embedding_model("https://api.minimax.chat/v1") == "embo-01"
    # 未知服务商给通用默认（OpenAI 兼容接口通用）
    assert default_embedding_model("https://llm.example.com/v1") == EMBED_MODEL_FALLBACK


def test_no_hardcoded_minimax_fallback():
    """兜底值不得再是 MiniMax 专属模型（换服务商就静默失效的根源）。"""
    from app.services import llm_gateway

    src = open(llm_gateway.__file__, encoding="utf-8").read()
    # embed() 里不得再出现 `or "embo-01"` 这种硬编码回落
    assert 'or "embo-01"' not in src, "embed() 仍硬编码 embo-01 兜底（D163-2）"


@pytest.mark.asyncio
async def test_embed_uses_configured_model_first(monkeypatch):
    """配置里显式填了 embedding_model → 用它（优先级最高）。"""
    from app.services import llm_gateway

    cfg = type("C", (), {})()
    cfg.extra_config = {"embedding_model": "my-embed-v9"}
    cfg.base_url = "https://llm-xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    cfg.secret_encrypted = "x"
    cfg.enabled = True

    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"embedding": [0.1] * 1536}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen["payload"] = json
            seen["url"] = url
            return _Resp()

    async def _fake_pick(db, role="fast"):
        return cfg

    monkeypatch.setattr(llm_gateway, "pick_config", _fake_pick)
    monkeypatch.setattr(llm_gateway, "_api_key_of", lambda c: "k")
    monkeypatch.setattr(llm_gateway, "enforce_quota", lambda db, role="": _noop())
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: _Client())

    out = await llm_gateway.embed(None, ["测试"])
    assert len(out) == 1 and len(out[0]) == 1536
    assert seen["payload"]["model"] == "my-embed-v9", seen["payload"]


@pytest.mark.asyncio
async def test_embed_falls_back_to_provider_default(monkeypatch):
    """配置为空串（=没配）→ 按 base_url 推断，不再硬编码 embo-01。"""
    from app.services import llm_gateway

    cfg = type("C", (), {})()
    cfg.extra_config = {"embedding_model": ""}   # 公网的真实情况：空串
    cfg.base_url = "https://llm-xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    cfg.secret_encrypted = "x"
    cfg.enabled = True

    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"embedding": [0.3] * 1536}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen["payload"] = json
            return _Resp()

    async def _fake_pick(db, role="fast"):
        return cfg

    monkeypatch.setattr(llm_gateway, "pick_config", _fake_pick)
    monkeypatch.setattr(llm_gateway, "_api_key_of", lambda c: "k")
    monkeypatch.setattr(llm_gateway, "enforce_quota", lambda db, role="": _noop())
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: _Client())

    out = await llm_gateway.embed(None, ["测试"])
    assert len(out) == 1 and len(out[0]) == 1536
    assert seen["payload"]["model"] == "text-embedding-v1", seen["payload"]


@pytest.mark.asyncio
async def test_embed_model_not_found_gives_actionable_message(monkeypatch):
    """模型名不存在 → 报错要说清"是模型名的问题"，不是"响应形状不可识别"。"""
    from fastapi import HTTPException
    from app.services import llm_gateway

    cfg = type("C", (), {})()
    cfg.extra_config = {"embedding_model": "wrong-model"}
    cfg.base_url = "https://llm-xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    cfg.secret_encrypted = "x"
    cfg.enabled = True

    class _Resp:
        status_code = 404

        def json(self):
            return {"error": {"message": "Model not exist.",
                              "code": "model_not_found"}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    async def _fake_pick(db, role="fast"):
        return cfg

    monkeypatch.setattr(llm_gateway, "pick_config", _fake_pick)
    monkeypatch.setattr(llm_gateway, "_api_key_of", lambda c: "k")
    monkeypatch.setattr(llm_gateway, "enforce_quota", lambda db, role="": _noop())
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: _Client())

    with pytest.raises(HTTPException) as ei:
        await llm_gateway.embed(None, ["测试"])
    msg = str(ei.value.detail)
    assert "wrong-model" in msg, msg
    assert "不存在" in msg, msg
    assert "embedding_model" in msg, "应告诉用户去哪儿配"
    assert "响应形状不可识别" not in msg, "旧的误导性文案不该再出现"


def _noop():
    async def _n(*a, **k):
        return None
    return _n()


@pytest.mark.asyncio
async def test_embed_rejects_wrong_dimension(monkeypatch):
    """D163-2：维度与向量列（VECTOR(1536)）不符 → 提前拦下并说清换模型。

    场景：阿里云 text-embedding-v3 默认输出 1024 维（实测 dimensions=1536 被拒），
    写进 VECTOR(1536) 会失败——那时错误在写库层，离根因很远。
    """
    from fastapi import HTTPException
    from app.services import llm_gateway

    cfg = type("C", (), {})()
    cfg.extra_config = {"embedding_model": "text-embedding-v3"}
    cfg.base_url = "https://llm-xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    cfg.secret_encrypted = "x"
    cfg.enabled = True

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"embedding": [0.1] * 1024}]}   # 1024 维，与 VECTOR(1536) 不符

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    async def _fake_pick(db, role="fast"):
        return cfg

    monkeypatch.setattr(llm_gateway, "pick_config", _fake_pick)
    monkeypatch.setattr(llm_gateway, "_api_key_of", lambda c: "k")
    monkeypatch.setattr(llm_gateway, "enforce_quota", lambda db, role="": _noop())
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: _Client())

    with pytest.raises(HTTPException) as ei:
        await llm_gateway.embed(None, ["测试"])
    msg = str(ei.value.detail)
    assert "EMBED_DIM_MISMATCH" in msg, msg
    assert "1024" in msg and "1536" in msg, msg
    assert "text-embedding-v1" in msg, "应提示可用哪个模型"
