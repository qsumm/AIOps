"""对话上下文管理：滑动窗口 + 滚动摘要（hybrid 记忆）。

## 为什么有这个模块（用户要求："看看通用的大模型对话类上下文是怎么管理的，完善下"）

改动前的实现是**把整段历史原样丢给模型**：`sess.messages` 有多少条就发多少条。它短会话没问题，
但有三处会随会话变长而恶化：
① 每次请求的 prompt 线性增长 → 费用与首字延迟一起涨，最终**撞上下文上限直接报错**（对话直接不可用）；
② 没有预算概念，粘贴一大段日志就能把窗口占满；
③ 历史里只有"一问一答"，中间的工具结果不入库，模型看不到自己查过什么，容易反复查同一件事。

## 通用做法（调研结论，三条主干）

| 做法 | 机制 | 代价 | 问题 |
|---|---|---|---|
| 滑动窗口 | 只保留最近若干条，其余丢弃 | 零成本、零延迟 | 丢长期信息（用户早先说的约束会被忘掉）|
| 摘要压缩 | 旧消息交模型压成一段摘要 | 多一次模型调用（钱 + 延迟）| 每次都重压全量则成本高 |
| **混合（业界共识，本模块采用）** | **滚动摘要 + 最近窗口**，按 token 预算触发，**增量**更新 | 一次调用只压溢出那一段 | 摘要有损，需如实告知用户 |

配套的三条工程纪律（同样来自调研，逐条落到了代码里）：
1. **不切断配对**：窗口只能从"用户发言"开始，绝不把 assistant 回答和它的提问拆到两侧；
2. **为回复预留输出**：预算算的是**历史**部分，system 与模型输出另算（见 `chat_context_budget` 的帮助文本）；
3. **失败要如实说**：压缩调用失败时，往上下文里写"更早的 N 轮未能压缩、本轮不参与上下文"，
   绝不假装还记得（这条与平台"不杜撰"的纪律一致）。

另外两处是"通用对话产品"的常识，也一并做了：单条消息长度上限（防一段巨文本撑爆窗口）、
单会话存储上限（防止 DB 里一行 JSON 无限增长）。
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("chat_context")

# 中文/全角字符 ≈ 1 token/字；其余（ASCII/空格/标点）≈ 0.3 token/字符。
# 不引入 tiktoken：模型是可配置的（OpenAI 兼容任意后端），tokenizer 未必对得上；
# 这个启发式有 20%~30% 余量就够了——**它只用做分档判断，不用来精确计费**（计费走 API 返回的真实 usage）。
_CJK = re.compile(r"[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]")
_ROLE_OVERHEAD = 4  # 每条消息的角色标签/分隔符开销（粗略）

# 窗口最少保留几条：预算再紧也不能把上下文清成"只剩这一句"，
# 否则模型会失去对上一轮的全部记忆（用户会感觉"助手突然失忆"）。
_MIN_KEEP_MESSAGES = 4


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文按字、其余按 0.3）。仅用于分档，不用于计费。"""
    s = str(text or "")
    if not s:
        return 0
    cjk = len(_CJK.findall(s))
    return int(cjk + (len(s) - cjk) * 0.3) + 1


def _msg_tokens(m: dict) -> int:
    return _ROLE_OVERHEAD + estimate_tokens(m.get("content") or "")


def total_tokens(messages: list[dict]) -> int:
    return sum(_msg_tokens(m) for m in messages)


def split_window(messages: list[dict], start: int, budget: int) -> tuple[int, dict]:
    """把 `messages[start:]` 切成「溢出段」与「保留窗口」，返回 (窗口起点, 统计)。

    - `messages[start:kept]` = 溢出段（可折叠进滚动摘要）
    - `messages[kept:]` = 保留窗口（原样发给模型）

    规则（按优先级）：
    1. **最后一条必须保留**（那是用户刚问的话，丢了就没法回答）；
    2. 从最新往回累加，超出 `budget` 就停；
    3. 至少保留 `_MIN_KEEP_MESSAGES` 条（预算再紧也留一点上下文）；
    4. **窗口从 user 发言开始**（不把回答与它的提问拆开）。
    """
    n = len(messages)
    start = max(0, min(start, n))
    if start >= n:
        return n, {"kept": 0, "kept_tokens": 0, "overflow_tokens": 0, "folded_candidates": 0}

    used = 0
    kept = n
    for i in range(n - 1, start - 1, -1):
        t = _msg_tokens(messages[i])
        # `n - kept` 是**当前窗口已有几条**：达到下限后才允许因超预算停下。
        # 最后一条（kept==n）无条件先收进来——那是用户刚问的话。
        if kept < n and (n - kept) >= _MIN_KEEP_MESSAGES and used + t > budget:
            break
        used += t
        kept = i
    # 规则 4：窗口起点必须是 user（否则把 assistant 的孤立回答留在开头）
    while kept < n and messages[kept].get("role") != "user":
        kept += 1
    overflow = messages[start:kept]
    return kept, {
        "kept": n - kept,
        "kept_tokens": total_tokens(messages[kept:]),
        "overflow_tokens": total_tokens(overflow),
        "folded_candidates": len(overflow),
    }


_SUMMARY_SYS = (
    "你在压缩一段运维平台 AI 助手的对话历史。把下面的对话压成**要点清单**，供后续对话继续使用。"
    "必须保留：① 用户提出的目标与约束；② 已确认的事实（含具体读数/主机名/时间，数字不要改写）；"
    "③ 已排除的假设与原因；④ 尚未解决的问题。**不要编造**摘要里没有的内容；不确定的宁可不写。"
    "输出纯文本要点，不要客套话。"
)


async def maybe_compact(db, messages: list[dict], summary: str, upto: int, budget: int,
                        threshold: int, quota_role: str = "") -> tuple[str, int, dict]:
    """按预算判断是否需要压缩；需要则**增量**压掉溢出段（旧摘要 + 溢出段 → 新摘要）。

    返回 `(summary, upto, stats)`。`upto` 是"已被摘要覆盖的消息条数"——它是 messages 数组的下标，
    所以 `messages` **展示用全量保留**（用户回看历史不会变短），而发给模型的只有 `messages[upto:]`。
    """
    kept, stats = split_window(messages, upto, budget)
    stats["compacted"] = False
    if kept <= upto or stats["overflow_tokens"] < threshold:
        return summary, upto, stats  # 还不到该压的时候（省钱：绝大多数轮次走这里）

    seg = messages[upto:kept]
    try:
        from app.services.llm_gateway import call_llm, strip_think

        payload = json.dumps({"已有摘要": summary, "需要并入的对话": seg},
                             ensure_ascii=False, default=str)
        r = await call_llm(db, "summary", [
            {"role": "system", "content": _SUMMARY_SYS},
            {"role": "user", "content": payload[:60000]},
        ], model_role="fast", quota_role=quota_role)
        new_summary = strip_think(r.content).strip()
        if not new_summary:
            raise ValueError("摘要为空")
        stats["compacted"] = True
        log.info("对话上下文压缩：折叠 %d 条（%d token）→ 摘要 %d 字",
                 len(seg), stats["overflow_tokens"], len(new_summary))
        return new_summary, kept, stats
    except Exception as e:  # noqa: BLE001 —— 压缩失败绝不阻断对话
        # 纪律 3：失败要如实写进上下文，不假装有记忆
        note = (f"（更早的 {len(seg)} 轮对话压缩失败：{type(e).__name__}，"
                f"这部分内容本轮不参与上下文，如需请再说明一次。）")
        log.warning("对话上下文压缩失败（%s），已如实降级", type(e).__name__)
        return (summary + "\n" + note).strip() if summary else note, kept, stats


def build_history(system_prompt: str, summary: str, messages: list[dict], upto: int,
                  budget: int) -> list[dict]:
    """拼出真正发给模型的消息列表：system + （滚动摘要）+ 最近窗口。"""
    kept, _ = split_window(messages, upto, budget)
    hist: list[dict] = [{"role": "system", "content": system_prompt}]
    if summary:
        hist.append({"role": "system",
                     "content": "## 更早对话的摘要（已被压缩，作为背景；与你直接看到的历史同等可信）\n" + summary})
    for m in messages[kept:]:
        hist.append({"role": m.get("role", "user"), "content": m.get("content") or ""})
    return hist


def trim_stored(messages: list[dict], upto: int, cap: int) -> tuple[list[dict], int]:
    """单会话存储上限：超了就从**最前面**裁掉（那部分早已在摘要里），并把摘要边界前移。

    注意裁的是"展示用的全量历史"的最后手段——正常路径下压缩已经让**发给模型**的部分有界，
    这里只是防止 DB 里一行的 JSON 无限增长（长跑稳定性）。
    裁完的总条数（含那条如实说明）不超过 `cap`。
    """
    if cap <= 0 or len(messages) <= cap:
        return messages, upto
    keep_n = max(1, cap - 1)          # 留 1 个名额给"已归档"说明行
    drop = len(messages) - keep_n
    note = {"role": "system", "content": f"（更早的 {drop} 条对话已超出保留上限并归档到摘要）"}
    return [note] + messages[drop:], max(0, upto - drop + 1)
