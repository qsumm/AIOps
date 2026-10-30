"""SSE 建流鉴权：一次性 ticket（FR-C7；03 §1.22）。

EventSource 无法自定义 header，故先以 Bearer JWT 换一次性 ticket（60s、绑定 user），
再以 ?ticket= 建流；服务端校验后立即删除（消费即焚）。
"""
import secrets
import time

_TICKETS: dict[str, dict] = {}
TTL_S = 60


def mint(user_id: int, role: str) -> str:
    _gc()
    t = secrets.token_hex(32)
    _TICKETS[t] = {"user_id": user_id, "role": role, "exp": time.time() + TTL_S}
    return t


def consume(ticket: str) -> dict | None:
    """校验并消费（消费即焚）。返回 {user_id, role} 或 None。"""
    rec = _TICKETS.pop(ticket or "", None)
    if rec is None or rec["exp"] < time.time():
        return None
    return {"user_id": rec["user_id"], "role": rec["role"]}


def _gc() -> None:
    now = time.time()
    for k in [k for k, v in _TICKETS.items() if v["exp"] < now]:
        _TICKETS.pop(k, None)
