"""案例卡：向量召回 + 文本降级（FR-E3；03 §1.19）。

case_cards 全部走 raw SQL（表含 VECTOR 列，ORM 反射会崩，04 §10）：
- 写入：embedding 以 JSON 文本参数化插入（SeekDB 实测可行）
- 召回：优先 `ORDER BY l2_distance(embedding, ?)`（标准路径）；向量不可用 → LIKE 文本匹配（降级开关）

降级策略（测试阶段修正）：向量失败只**冷却 5 分钟**再重试，不再永久锁死。
原实现把 VECTOR_ENABLED 一票否决到底，一次瞬时失败（网络抖动／密钥未就绪）会让本进程
此后所有案例写入都不带向量、召回全走文本，且没有任何自愈路径——D8 实测就是这么暴露的。
VECTOR_ENABLED 保留为硬开关（测试可 monkeypatch 强制降级）。
"""
from __future__ import annotations

import json
import logging
import time

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.llm_gateway import embed

log = logging.getLogger("cases")

VECTOR_ENABLED = True      # 硬开关：False 时一律走文本召回
VECTOR_COOLDOWN_S = 300    # 向量失败后的冷却时长（到点自动重试）
_vector_retry_at = 0.0     # 冷却截止时间（monotonic）


def _vector_ready() -> bool:
    return VECTOR_ENABLED and time.monotonic() >= _vector_retry_at


def _vector_failed(where: str, exc: Exception) -> None:
    """标记向量通道暂时不可用。注意 str(e) 可能为空（如 cryptography InvalidTag），须带类型名。"""
    global _vector_retry_at
    _vector_retry_at = time.monotonic() + VECTOR_COOLDOWN_S
    log.warning("向量通道暂不可用（%s），降级文本匹配 %ss 后自动重试：%s: %s",
                where, VECTOR_COOLDOWN_S, type(exc).__name__, exc or "(无消息)")


# 参与向量的文本至少要有这么多字符，否则**不写向量**（D69）
# 实测背景：测试期留下的一张「CPU 高负载」卡（5 字泛文本）在向量空间里成了"黑洞"——
# 磁盘类、连接类的改写查询全被它吸走，Top-1 命中率从 78% 掉到 67%。
# 短泛文本的向量落在空间中心附近，会拖累**所有**查询；不参与向量检索反而更干净
# （卡仍在库里、仍能被文本关键词召回，只是不污染语义检索）。
_MIN_EMBED_CHARS = 16


async def create_case_card(incident_id: int | None, symptom: str, root_cause: str, resolution_md: str,
                           tags: list[str] | None = None, embed_text: str | None = None) -> int | None:
    """写案例卡（含 embedding）。向量不可用时仍写卡，只是 embedding 为 NULL。

    `embed_text`（D69）：**参与向量计算的文本**可与展示字段解耦。自动沉淀的案例卡
    `symptom` 取事件标题（如「演示-探活失败 · xxx」），短且含主机名/规则名——直接拿它
    做向量会退化成泛化文本（效果见 `_MIN_EMBED_CHARS` 注释）。调用方可传更丰富的文本
    （标题 + 根因 + 报告摘要）来生成向量，而列表里展示的仍是简洁的标题。
    """
    vec = None
    # 注意变量名：不能叫 text —— 会遮蔽 sqlalchemy 的 text() 导入（D69 自查抓到的坑）
    embed_input = (embed_text or f"{symptom} {root_cause}").strip()[:1500]
    if len(embed_input) < _MIN_EMBED_CHARS:
        log.info("案例卡文本过短（%d 字），跳过向量：%s", len(embed_input), embed_input[:20])
    elif _vector_ready():
        try:
            async with SessionLocal() as db:
                vec = (await embed(db, [embed_input]))[0]
        except Exception as e:  # noqa: BLE001 —— 降级：不写向量
            _vector_failed("写入embedding", e)
    async with SessionLocal() as db:
        r = await db.execute(text(
            "INSERT INTO case_cards (incident_id, symptom, root_cause, resolution_md, tags, embedding) "
            "VALUES (:i, :s, :rc, :res, :t, :e)"),
            {"i": incident_id, "s": symptom, "rc": root_cause, "res": resolution_md,
             "t": json.dumps(tags or [], ensure_ascii=False),
             "e": json.dumps(vec) if vec else None})
        await db.commit()
        return r.lastrowid


async def recall(query: str, top_k: int = 3, quota_role: str = "") -> dict:
    """召回历史案例。返回 {"items": [...], "mode": "vector"|"text"|"none", "note": str}。"""
    if _vector_ready():
        try:
            async with SessionLocal() as db:
                qv = (await embed(db, [query[:1500]], quota_role=quota_role))[0]
                rows = (await db.execute(text(
                    "SELECT id, symptom, root_cause, resolution_md, l2_distance(embedding, :q) AS dist "
                    "FROM case_cards WHERE embedding IS NOT NULL AND enabled = 1 ORDER BY dist LIMIT :k"),
                    {"q": json.dumps(qv), "k": max(1, min(top_k, 10))})).all()
            if rows:
                return {"items": [{"id": r[0], "symptom": r[1], "root_cause": r[2],
                                   "resolution_md": r[3], "distance": round(float(r[4]), 4)} for r in rows],
                        "mode": "vector", "note": f"SeekDB l2_distance 向量召回 top{len(rows)}"}
        except Exception as e:  # noqa: BLE001 —— 降级文本匹配
            _vector_failed("向量召回", e)
            return await _text_recall(query, top_k, reason=f"向量不可用（{type(e).__name__}）")
    return await _text_recall(query, top_k, reason="向量冷却中" if VECTOR_ENABLED else "向量已关闭")


async def _text_recall(query: str, top_k: int, reason: str = "") -> dict:
    kw = (query or "").strip()[:30]
    async with SessionLocal() as db:
        try:
            rows = (await db.execute(text(
                "SELECT id, symptom, root_cause, resolution_md FROM case_cards "
                "WHERE enabled = 1 AND (symptom LIKE :q OR root_cause LIKE :q OR resolution_md LIKE :q) LIMIT :k"),
                {"q": f"%{kw}%", "k": max(1, min(top_k, 10))})).all()
        except Exception:  # noqa: BLE001 —— 表不存在
            return {"items": [], "mode": "none", "note": "案例库不可用"}
    if not rows:
        return {"items": [], "mode": "text", "note": f"文本匹配无命中（{reason}）"}
    return {"items": [{"id": r[0], "symptom": r[1], "root_cause": r[2], "resolution_md": r[3]} for r in rows],
            "mode": "text", "note": f"文本匹配（{reason}）"}


async def list_for_incident(incident_id: int) -> list[dict]:
    """按事件查案例卡（UI 展示人工订正用）。raw SQL：case_cards 含 VECTOR 列，ORM 反射会崩。"""
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT id, symptom, root_cause, resolution_md, tags, created_at "
            "FROM case_cards WHERE incident_id=:i ORDER BY id DESC"), {"i": incident_id})).all()
    out = []
    for r in rows:
        try:
            tags = json.loads(r[4]) if isinstance(r[4], str) else (r[4] or [])
        except Exception:
            tags = []
        out.append({"id": r[0], "symptom": r[1], "root_cause": r[2], "resolution_md": r[3],
                    "tags": tags, "created_at": str(r[5])})
    return out


async def case_count(enabled_only: bool = True) -> int:
    sql = "SELECT COUNT(*) FROM case_cards" + (" WHERE enabled = 1" if enabled_only else "")
    async with SessionLocal() as db:
        try:
            return int((await db.execute(text(sql))).scalar() or 0)
        except Exception:  # noqa: BLE001
            return 0


async def _embed_for(symptom: str, root_cause: str) -> list | None:
    """按同一套纪律算向量：文本太短就不写（避免泛化文本污染检索，D69）。"""
    embed_input = f"{symptom} {root_cause}".strip()[:1500]
    if len(embed_input) < _MIN_EMBED_CHARS:
        log.info("案例卡文本过短（%d 字），清空向量：%s", len(embed_input), embed_input[:20])
        return None
    if not _vector_ready():
        return None
    try:
        async with SessionLocal() as db:
            return (await embed(db, [embed_input]))[0]
    except Exception as e:  # noqa: BLE001
        _vector_failed("更新embedding", e)
        return None


async def update_case_card(cid: int, *, symptom: str | None = None, root_cause: str | None = None,
                           resolution_md: str | None = None, tags: list | None = None,
                           enabled: bool | None = None) -> bool:
    """编辑案例卡。**改了症状/根因就重算向量**——否则向量与文本不一致，
    检索会把这张卡匹配到它其实并不相关的问题上（比没有向量更糟）。

    文本被改短到阈值以下时**清空向量**（与写入时同一套纪律）：卡还在、还能被关键词召回，
    只是不再参与语义检索。
    """
    sets, params = [], {"id": cid}
    reembed = symptom is not None or root_cause is not None
    if symptom is not None:
        sets.append("symptom = :s")
        params["s"] = symptom.strip()
    if root_cause is not None:
        sets.append("root_cause = :rc")
        params["rc"] = root_cause.strip()
    if resolution_md is not None:
        sets.append("resolution_md = :res")
        params["res"] = resolution_md
    if tags is not None:
        sets.append("tags = :t")
        params["t"] = json.dumps(tags, ensure_ascii=False)
    if enabled is not None:
        sets.append("enabled = :en")
        params["en"] = 1 if enabled else 0
    if not sets and not reembed:
        return False

    async with SessionLocal() as db:
        if reembed:
            cur = (await db.execute(text(
                "SELECT symptom, root_cause FROM case_cards WHERE id = :id"), {"id": cid})).all()
            if not cur:
                return False
            sym = (params.get("s") or cur[0][0] or "").strip()
            rc = (params.get("rc") or cur[0][1] or "").strip()
            vec = await _embed_for(sym, rc)
            sets.append("embedding = :e")
            params["e"] = json.dumps(vec) if vec else None
        r = await db.execute(text(f"UPDATE case_cards SET {', '.join(sets)} WHERE id = :id"), params)
        await db.commit()
        # 必须看 rowcount：不存在的 id 也要如实返回 False（否则接口会对一个不存在的卡回 200，
        # 用户以为改成功了）——实测就是这个断言抓到的
        return bool(r.rowcount)


async def delete_case_card(cid: int) -> bool:
    async with SessionLocal() as db:
        r = await db.execute(text("DELETE FROM case_cards WHERE id = :id"), {"id": cid})
        await db.commit()
        return bool(r.rowcount)
