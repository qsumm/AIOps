# ymaicode © 一鸣 | d367e532b17f234b8b79094754189103 | https://github.com/qsumm/TestOPS
"""LLM 网关：统一入口 call_llm + 配置 CRUD 逻辑（FR-A2；03 §1.3）。

- purpose 计量分账（rca/chat/summary/inspect/fixcheck/embed）
- 双模型：model_role="main"|"fast"（fast 取 cfg.fast_model，缺省回落主模型）
- api_key AES-GCM 加密落库（key_store_secret）
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.llm import LLMConfig, LLMUsage

log = logging.getLogger("llm_gateway")
settings = get_settings()


# ---------- api_key 加密 ----------
def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    key = settings.key_store_secret.encode()[:32].ljust(32, b"\0")
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plain.encode(), None)
    return nonce.hex() + "." + ct.hex()


def decrypt_secret(stored: str) -> str:
    if not stored:
        return ""
    try:
        nonce_hex, ct_hex = stored.split(".", 1)
        key = settings.key_store_secret.encode()[:32].ljust(32, b"\0")
        return AESGCM(key).decrypt(bytes.fromhex(nonce_hex), bytes.fromhex(ct_hex), None).decode()
    except Exception:  # noqa: BLE001 —— KEY_STORE_SECRET 与写库时不符等：降级 .env 明文，绝不因解密崩掉整个 LLM
        log.warning("模型 API key 解密失败（KEY_STORE_SECRET 与写库时不匹配？），回退 .env 明文")
        return ""


# ---------- 配置选择 ----------
class _EnvConfig:
    """无 DB 配置时的兜底（settings.llm_*），让 demo/dev 开箱可用。"""

    def __init__(self, s):
        self.id = 0
        self.name = "env-fallback"
        self.base_url = s.llm_base_url
        self.api_key_encrypted = ""
        self.model = s.llm_model_main or "MiniMax-M3"
        self.fast_model = s.llm_model_fast
        self.is_default = True
        self.enabled = True
        self.extra_config = s.llm_extra or {}
        self._plain_key = s.llm_api_key


async def pick_config(db, model_role: str = "main"):
    cfgs = (await db.execute(select(LLMConfig).where(LLMConfig.enabled.is_(True)))).scalars().all()
    if not cfgs:
        if settings.llm_api_key:
            return _EnvConfig(settings)
        raise HTTPException(503, "LLM_UNAVAILABLE：未配置可用模型，请在系统设置页添加")
    default = next((c for c in cfgs if c.is_default), cfgs[0])
    if model_role == "fast" and default.fast_model:
        return default  # fast_model 字段在调用时替换 model 名（03 §1.3）
    return default


async def pick_fallback_config(db):
    """取**用户显式指定**的降级通道配置。

    D163-3（用户明确纠正）：降级目标**必须由用户指定**，平台不能替用户挑。
    原实现降级时隐式改用同一配置的 `fast_model` —— 用户配 fast_model 是为了
    "轻量任务用快模型"，**不等于**允许它在主模型额度耗尽时兜底
    （配额、合规、计费口径都可能不允许）。用户原话：
    "万一 qwen3.8-flash 用户不允许用呢"。

    现在规则：只有**勾了「作为降级通道」**的配置才会被用作降级；
    一个都没配 → 如实报错，让用户知道要配，而不是擅自用某个模型顶上。
    """
    cfg = (await db.execute(
        select(LLMConfig).where(
            LLMConfig.enabled.is_(True),
            LLMConfig.fallback_enabled.is_(True),
        ).order_by(LLMConfig.id))).scalars().first()
    if cfg is None:
        raise HTTPException(503, (
            "LLM_FALLBACK_UNCONFIGURED：AI 服务今日额度已用完（服务商限流/账户额度耗尽），"
            "而平台**没有配置降级通道**——请在「系统设置 → 模型配置」里"
            "给一个可用的模型配置勾上「作为降级通道」（可与服务商不同）。"
            "平台不会擅自改用其他模型，因为那未必是允许使用的。"))
    return cfg


def _api_key_of(cfg) -> str:
    plain = getattr(cfg, "_plain_key", None)
    if plain:
        return plain
    dec = decrypt_secret(cfg.api_key_encrypted)
    # DB 行 key 解不开时回退 .env 明文（与 _EnvConfig 兜底同源）；都没有才用占位。
    return dec or (settings.llm_api_key or "") or "sk-none"


def strip_think(text: str) -> str:
    """剥离推理模型的内联思考段（MiniMax-M3 返回 <think>…</think>，D4 实测发现）。"""
    import re

    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S).strip()


class LLMResult:
    def __init__(self, content: str, usage: dict, model: str, tool_calls=None, finish_reason: str = ""):
        self.content, self.usage, self.model = content, usage, model
        self.tool_calls = tool_calls or []       # openai SDK 的 tool_calls 原对象列表（D4 ReAct 需要）
        self.finish_reason = finish_reason


async def call_llm(
    db,
    purpose: str,
    messages: list[dict],
    tools: list | None = None,
    stream: bool = False,
    model_role: str = "main",
    timeout_s: int = 600,
    quota_role: str = "",
    temperature: float | None = None,   # FR-428a：评测回放传 0（确定性）；None=沿用配置
) -> LLMResult:
    """统一 LLM 入口。失败重试 1 次（指数退避）→ LLMUnavailable（503）。

    quota_role：调用方角色（"demo" 时套用更严的演示限额）。见 enforce_quota。

    D163-3：**主模型额度耗尽时降级到"用户显式指定的降级通道"**。
      公网实测：主模型 qwen3.8-max 的服务商免费额度用尽（403 Free quota exhausted），
      原实现此时让 AI 助手**整个不可用**。

      降级目标**必须由用户指定**（用户明确纠正："万一 qwen3.8-flash 用户不允许用呢"）：
      降级走 `llm_configs.fallback_enabled=True` 的那条配置（可与主模型不同服务商、
      不同模型、不同 key），**不擅自**改用同一配置的 fast_model —— 那是"轻量任务
      用的快模型"，用户配它不等于允许它兜底。没配降级通道就如实报错。
      降级只在「服务商侧额度/限流」时做（模型不存在/密钥错是配置问题，重试无用）。
    """
    try:
        return await _call_llm_once(db, purpose, messages, tools, model_role, timeout_s, quota_role,
                                   temperature=temperature)
    except _FallbackToFastModel as fb:
        log.warning("主模型不可用（%s），尝试降级到用户配置的降级通道", fb.reason)
        fb_reason_user = _short_reason(fb.original)
        cfg = await pick_fallback_config(db)   # 没配 → 抛 LLM_FALLBACK_UNCONFIGURED（人话）
        # 降级通道用**该配置里更轻量的模型**：配了 fast_model 就用它（那才是"这条配置
        # 里备着的另一个模型"），没配才用 model。实测：降级通道若配了 fast_model，
        # 走 model 会打回主模型同一个（额度耗尽的）模型 —— 白跑一次。
        fb_model = (getattr(cfg, "fast_model", "") or "").strip() or cfg.model
        # 降级通道与主配置是同一条（同一服务商/key/**同一个模型**）→ 降级无意义，白跑一次。
        # 注意比较的是**实际要用的模型名**：降级通道若配了 fast_model，用的是那个，
        # 与主模型的主模型名不同就不算重复。
        main_cfg = await pick_config(db, "main")
        fb_model_check = (getattr(cfg, "fast_model", "") or "").strip() or getattr(cfg, "model", "")
        same_base = (getattr(cfg, "base_url", "") == getattr(main_cfg, "base_url", "")
                     and getattr(cfg, "api_key_encrypted", "")
                     == getattr(main_cfg, "api_key_encrypted", ""))
        if same_base and fb_model_check == getattr(main_cfg, "model", ""):
            raise HTTPException(503, (
                "LLM_FALLBACK_SAME_AS_MAIN：配置的降级通道与主模型是同一个"
                f"（{getattr(cfg, 'name', '?')} / {fb_model_check}）——"
                "降级不会起作用。请把降级通道指向另一个服务商或另一个模型。"))
        try:
            r = await _call_llm_with_config(db, purpose, messages, tools, cfg, timeout_s,
                                            quota_role, model=fb_model, temperature=temperature)
        except _RawLLMError as raw:
            # 降级通道也失败 → 报它的人话提示（比主模型的原始异常更贴近当前故障）
            # 但要**说清是哪条通道**——否则用户会以为是主配置坏了。
            he = _http_error_for(raw.err)
            he.detail = (f"降级通道「{getattr(cfg, 'name', '?')}」也调用失败：{he.detail}"
                         f"（主模型失败原因：{fb_reason_user}）")
            raise he
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 —— 降级通道也失败 → 回到主模型的错误
            log.warning("降级通道也失败（%s）：%s", type(e).__name__, str(e)[:200])
            raise fb.original
        # 注意：降级**不新增 purpose**：llm_usage.purpose 是 ENUM(rca/chat/summary/
        #    inspect/fixcheck/embed/suggest)，塞 "chat.fallback" 会 DataError
        #    （列截断/枚举不匹配）→ 整个请求 500。降级事实由 **model 字段**体现
        #    （本来就记的是实际用的模型名），另在日志里留痕即可。
        log.info("主模型额度/限流不可用，本次已降级到通道「%s」的模型 %s（purpose=%s）",
                 getattr(cfg, "name", "?"), r.model, purpose)
        return r


class _FallbackToFastModel(Exception):
    """内部信号：主模型因服务商侧额度/限流不可用，应走用户配置的降级通道。"""

    def __init__(self, reason: str, original: Exception):
        super().__init__(reason)
        self.reason = reason
        self.original = original


def _short_reason(err: Exception | None) -> str:
    """把服务商原始异常压成**不含原始 JSON/堆栈**的一句话（用户可见）。

    D98 纪律：用户界面上的文案不得含原始报错细节（openai 的异常 str() 里是
    完整 JSON + 英文堆栈）。这里只取"哪个模型 + 哪类问题"。
    """
    if err is None:
        return "未知原因"
    name = type(err).__name__
    raw = str(err)
    if "403" in raw or "quota exhausted" in raw.lower() or "PermissionDenied" in name:
        return "服务商账户额度耗尽（403）"
    if "429" in raw or "RateLimit" in name:
        return "服务商限流（429）"
    if "402" in raw or "insufficient_balance" in raw.lower():
        return "服务商账户余额不足（402）"
    if "401" in raw or "Authentication" in name:
        return "接口密钥无效（401）"
    if "404" in raw or "model_not_found" in raw.lower():
        return "模型不存在（404）"
    return name


def _is_quota_or_rate_limit(err: Exception) -> bool:
    """判断错误是不是「服务商侧额度/限流」——只有这类才值得降级重试。

    模型不存在（404 model_not_found）、密钥错（401）、参数错（400）都是**配置问题**，
    降级到快模型也解决不了（快模型同样会 404/401），不该掩盖真实错误。
    """
    name = type(err).__name__
    raw = str(err)[:300]
    if "RateLimit" in name or "429" in raw:
        return True
    if "PermissionDenied" in name or "403" in raw or "quota exhausted" in raw.lower():
        return True
    if "insufficient_quota" in raw.lower() or "exceeded" in raw.lower() and "quota" in raw.lower():
        return True
    # 402 / insufficient_balance：服务商账户余额不足（如公网的 mimo 通道 402）——
    # 同样是"这个通道现在用不了"，换用户配置的降级通道是有意义的。
    if "402" in raw or "insufficient_balance" in raw.lower() or "insufficient balance" in raw.lower():
        return True
    return False


async def _call_llm_once(
    db,
    purpose: str,
    messages: list[dict],
    tools: list | None,
    model_role: str,
    timeout_s: int,
    quota_role: str,
    temperature: float | None = None,
) -> LLMResult:
    await enforce_quota(db, quota_role)
    cfg = await pick_config(db, model_role)
    model = cfg.model if model_role == "main" or not cfg.fast_model else cfg.fast_model
    try:
        return await _call_llm_with_config(db, purpose, messages, tools, cfg, timeout_s,
                                           quota_role, model=model, temperature=temperature)
    except _RawLLMError as raw:
        # D163-3：主模型因服务商额度/限流不可用 → 抛降级信号（降级通道可能额度独立）。
        if model_role == "main" and _is_quota_or_rate_limit(raw.err):
            raise _FallbackToFastModel(f"{type(raw.err).__name__}: {str(raw.err)[:120]}", raw.err)
        raise _http_error_for(raw.err)


async def _call_llm_with_config(
    db,
    purpose: str,
    messages: list[dict],
    tools: list | None,
    cfg,
    timeout_s: int,
    quota_role: str,
    model: str | None = None,
    temperature: float | None = None,
) -> LLMResult:
    """用**指定配置**调用模型（不经过 pick_config 的角色选择）。

    D163-3：降级通道是用户显式指定的一条独立配置（可与主模型不同服务商/key/模型），
    所以降级路径直接用它的 base_url + key + model，不走 fast_model 替换逻辑。
    """
    from openai import AsyncOpenAI

    model = model or cfg.model
    client = AsyncOpenAI(base_url=cfg.base_url or None, api_key=_api_key_of(cfg))

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            t0 = time.time()
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools or None,
                timeout=timeout_s,
                temperature=temperature if temperature is not None
                             else (cfg.extra_config or {}).get("temperature", 0.2),
            )
            usage = {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(resp.usage, "total_tokens", 0) or 0,
                "latency_ms": int((time.time() - t0) * 1000),
            }
            db.add(LLMUsage(purpose=purpose, model=model, role=quota_role or "engine",
                            **{k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}))
            await db.commit()
            choice = resp.choices[0]
            content = strip_think(choice.message.content or "")
            if not content:  # 推理模型可能把过程放 reasoning_content（D4 实测：MiniMax-M3 内联 <think>）
                content = strip_think(getattr(choice.message, "reasoning_content", "") or "")
            return LLMResult(content, usage, model,
                             tool_calls=list(getattr(choice.message, "tool_calls", None) or []),
                             finish_reason=getattr(choice, "finish_reason", "") or "")
        except Exception as e:  # noqa: BLE001 —— 网关统一降级
            last_err = e
            await asyncio.sleep(0.5 * (2**attempt))  # async 上下文，不得用阻塞的 time.sleep
    raise _RawLLMError(last_err)


class _RawLLMError(Exception):
    """内部信号：携带服务商原始异常，由调用方决定降级或转人话提示。"""

    def __init__(self, err: Exception | None):
        super().__init__(str(err)[:200] if err else "unknown")
        self.err = err


def _http_error_for(err: Exception | None) -> HTTPException:
    """D98：把服务商原始报错转成**用户能懂的人话**——原始 JSON/英文堆栈只进日志，不进界面。

    实测（额度耗尽现场）：AI 助手曾把 "RateLimitError: Error code: 429 - {...}" 整段抛给用户。
    """
    name = type(err).__name__ if err else ""
    # D163-3：分类必须看**完整**错误串——openai 的异常 str() 很长（前面是 403 + 完整 JSON），
    # 截断到 200 字符会把 "quota exhausted" 这类关键信息切掉 → 分类落进 else →
    # 用户看到"暂时不可用，请稍后重试"（实际重试无用，额度不会自己回来）。
    raw_full = str(err)
    log.warning("LLM 调用失败（%s）：%s", name, raw_full[:200])
    if "RateLimit" in name or "429" in raw_full:
        msg = ("AI 服务今日额度已用完（服务商限流）。监控与告警不受影响；"
               "诊断、AI 助手等依赖大模型的功能暂时不可用——明天自动恢复，或联系管理员升级套餐。")
    elif "402" in raw_full or "insufficient_balance" in raw_full.lower():
        msg = ("AI 服务商账户余额不足（服务商返回 402）。监控、告警、采集**不受影响**，"
               "但依赖大模型的功能暂时不可用——请联系管理员为该模型账户充值。")
    elif "PermissionDenied" in name or "403" in raw_full or "quota exhausted" in raw_full.lower():
        # D163-2：服务商侧**账户额度耗尽**（阿里云 MaaS "Free quota exhausted"）。
        # 此前落到 else 分支，提示"暂时不可用，请稍后重试"——**重试无用**（要充值），
        # 用户会一直等。单独给一条说清"要充值/关掉仅用免费额度模式"的提示。
        msg = ("AI 服务商账户额度已耗尽（服务商返回 403）。监控、告警、采集**不受影响**，"
               "但诊断与 AI 助手暂时不可用——请联系管理员为模型账户充值，"
               "或在服务商控制台关闭「仅用免费额度」模式；重试无用。")
    elif "Authentication" in name or "401" in raw_full or "invalid" in raw_full.lower() and "key" in raw_full.lower():
        msg = "AI 服务的接口密钥无效或未配置，请联系管理员在「系统设置 → 模型配置」中检查。"
    elif "timeout" in name.lower() or "timed out" in raw_full.lower() or "Connection" in name:
        msg = "AI 服务连接超时或不可达，请稍后重试；若持续失败请联系管理员检查网络与模型地址。"
    else:
        msg = "AI 服务暂时不可用，请稍后重试；若持续失败请联系管理员（模型服务返回异常）。"
    return HTTPException(503, msg)


# ---------- Embedding（FR-E3 案例向量召回）----------
EMBED_DIM = 1536  # 实测 MiniMax embo-01 输出维度；case_cards.embedding VECTOR(EMBED_DIM)

# D163-2：embedding 模型名**不能硬编码某一家服务商的模型**。
# 原实现回落值是 "embo-01"（MiniMax 的模型）—— 换服务商（如阿里云 MaaS）后
# 该模型不存在 → 每次调用 404 "Model not exist" → 向量召回**永久失效**，
# 只因有"降级为文本匹配"兜底才没暴露为报错（公网实测：案例库一直只走文本匹配）。
# 现在：配置里显式填 embedding_model 优先；否则**按 base_url 推断**服务商的默认模型。
#
# 注意：维度必须与 `case_cards.embedding VECTOR(1536)` 一致（EMBED_DIM）：
#   阿里云 text-embedding-v3 默认输出 **1024 维**且不支持 dimensions=1536（实测 400），
#   写进 VECTOR(1536) 会失败；**text-embedding-v1/v2 是 1536 维** —— 故这里取 v1。
EMBED_MODEL_BY_HOST = {
    "maas.aliyuncs.com": "text-embedding-v1",   # 阿里云百炼/MaaS（1536 维，与库列一致）
    "dashscope.aliyuncs.com": "text-embedding-v1",
    "openai.com": "text-embedding-3-small",     # 1536 维
    "api.minimax": "embo-01",                   # 1536 维
    "minimaxi.com": "embo-01",
}
EMBED_MODEL_FALLBACK = "text-embedding-3-small"  # OpenAI 兼容接口的通用默认（1536 维）


def default_embedding_model(base_url: str) -> str:
    """按 base_url 推断服务商的默认 embedding 模型名。"""
    b = (base_url or "").lower()
    for host, model in EMBED_MODEL_BY_HOST.items():
        if host in b:
            return model
    return EMBED_MODEL_FALLBACK


async def embed(db, texts: list[str], model_role: str = "fast", quota_role: str = "") -> list[list[float]]:
    """文本向量化。**兼容两种响应形状**（实测 MiniMax 非 OpenAI 兼容）：

    - OpenAI 兼容：请求 {model, input} → 响应 {data:[{embedding:[...]}]}
    - MiniMax 原生：请求 {model, texts, type} → 响应 {vectors:[[...]]}

    不可用时抛 HTTPException(503)，调用方降级为文本匹配（03 §3 降级总表）。
    """
    import httpx

    await enforce_quota(db)
    cfg = await pick_config(db, model_role)
    base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
    # D163-2：配置优先（空串也算没配），否则按服务商推断 —— 不再硬编码 embo-01。
    model = ((cfg.extra_config or {}).get("embedding_model") or "").strip() \
        or default_embedding_model(base)
    headers = {"Authorization": f"Bearer {_api_key_of(cfg)}", "Content-Type": "application/json"}

    async def _post(payload: dict):
        async with httpx.AsyncClient(timeout=60) as c:
            return await c.post(f"{base}/embeddings", json=payload, headers=headers)

    try:
        r = await _post({"model": model, "input": texts})
        body = r.json() if r.status_code < 500 else {}
        if isinstance(body.get("data"), list) and body["data"]:
            return _check_dim([list(map(float, item["embedding"])) for item in body["data"]], model)
        # D163-2：模型名不存在（404 model_not_found）是最常见原因，必须**说清楚**——
        # 原来一律报"响应形状不可识别"，排查时完全看不出是模型名配错。
        _err = (body.get("error") or {}) if isinstance(body, dict) else {}
        _code = _err.get("code") or ""
        if _code in ("model_not_found", "invalid_request_error") and "not exist" in str(_err.get("message", "")).lower():
            raise HTTPException(503, (
                f"EMBED_UNAVAILABLE：embedding 模型「{model}」在服务商侧不存在"
                f"（{_err.get('message')}）——请在「系统设置 → 模型配置」的 embedding_model "
                f"填该服务商支持的模型名，或留空由平台按 base_url 自动推断"))
        # 落到 MiniMax 原生形状（其 /v1/embeddings 不接受 OpenAI 的 input 字段）
        r2 = await _post({"model": model, "texts": texts, "type": "db"})
        body2 = r2.json()
        if isinstance(body2.get("vectors"), list) and body2["vectors"]:
            db.add(LLMUsage(purpose="embed", model=model, role=quota_role or "engine",
                            prompt_tokens=0, completion_tokens=0, total_tokens=len(texts)))
            await db.commit()
            return _check_dim([[float(x) for x in v] for v in body2["vectors"]], model)
        raise HTTPException(503, f"EMBED_UNAVAILABLE：响应形状不可识别 {str(body2)[:160]}")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"EMBED_UNAVAILABLE：{type(e).__name__}: {str(e)[:160]}") from e


def _check_dim(vecs: list[list[float]], model: str) -> list[list[float]]:
    """D163-2：校验向量维度与 `case_cards.embedding VECTOR(EMBED_DIM)` 一致。

    维度不匹配时**写库会失败**（MySQL/SeeKDB 报维度不符），但那时错误发生在
    写 case_cards 那一层，离根因（模型选了输出别的维度的）很远。
    这里提前拦下并说清：请换一个 1536 维的 embedding 模型。
    """
    for v in vecs:
        if len(v) != EMBED_DIM:
            raise HTTPException(503, (
                f"EMBED_DIM_MISMATCH：embedding 模型「{model}」输出 {len(v)} 维，"
                f"与案例库向量列（VECTOR({EMBED_DIM})）不符——写库会失败。"
                f"请改用输出 {EMBED_DIM} 维的 embedding 模型"
                f"（阿里云可用 text-embedding-v1/v2；text-embedding-v3 默认 1024 维）。"))
    return vecs


async def usage_summary(db, days: int = 7) -> list[dict]:
    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                func.date(LLMUsage.created_at).label("day"),
                LLMUsage.purpose,
                func.sum(LLMUsage.total_tokens),
            )
            .where(LLMUsage.created_at >= since)
            .group_by("day", LLMUsage.purpose)
        )
    ).all()
    return [{"day": str(r[0]), "purpose": r[1], "total_tokens": int(r[2] or 0)} for r in rows]


# ---------- 成本闸门：每日 token 限额（FR 非功能 §8.3）----------
def _today_start() -> datetime:
    return datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


async def tokens_used_today(db, role: str = "") -> int:
    """今日（UTC 自然日）已消耗 token。传 role 则只统计该角色的用量。

    D21 修复要点：原实现**永远统计全平台总量**，于是后台引擎自动产生的告警 AI 摘要
    （实测一天 593 次 / 28 万 token）与管理员自己的验收测试消耗，全都算进了
    "演示站防刷"的 20 万额度里 —— demo 用户自己几乎没用却被拦住（实测 760070/200000）。
    """
    q = select(func.coalesce(func.sum(LLMUsage.total_tokens), 0)).where(
        LLMUsage.created_at >= _today_start())
    if role:
        q = q.where(LLMUsage.role == role)
    return int((await db.execute(q)).scalar() or 0)


async def quota_state(db, role: str = "") -> dict:
    """{limit, used, remaining, exceeded, scope, resets_at}；limit=0 表示不限。

    demo 角色同时受 demo 限额与全局限额约束，取更严的一个。
    """
    demo_limit = int(settings.demo_daily_token_limit or 0)
    hard_limit = int(settings.llm_daily_token_limit or 0)
    if role == "demo" and demo_limit:
        limit = min(demo_limit, hard_limit) if hard_limit else demo_limit
        scope = "demo"
        used = await tokens_used_today(db, role="demo")   # ← 只看 demo 自己（D21 修复）
    else:
        limit, scope = hard_limit, "global"
        used = await tokens_used_today(db)                # ← 全平台（含 demo 与引擎），这是"平台成本上限"的本义
    reset = (_today_start() + timedelta(days=1)).isoformat() + "Z"
    return {"limit": limit, "used": used, "scope": scope, "resets_at": reset,
            "remaining": max(0, limit - used) if limit else None,
            "exceeded": bool(limit and used >= limit)}


async def enforce_quota(db, role: str = "") -> None:
    """超限抛 429（可读原因 + 重置时间）。每个 LLM 调用点前调用。"""
    st = await quota_state(db, role)
    if st["exceeded"]:
        who = "演示模式" if st["scope"] == "demo" else "平台"
        raise HTTPException(429, (
            f"LLM_QUOTA_EXCEEDED：{who}今日 token 已用尽（{st['used']}/{st['limit']}），UTC 次日自动重置。"
            "管理员可调整 LLM_DAILY_TOKEN_LIMIT / DEMO_DAILY_TOKEN_LIMIT（0=不限）"
        ))
