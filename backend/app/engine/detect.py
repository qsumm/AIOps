"""检测算法（FR-C3；03 §1.7）：threshold / 3sigma / isolation_forest（降级 3sigma）。"""
import re
from statistics import mean, stdev

_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: abs(a - b) < 1e-9,
    "!=": lambda a, b: abs(a - b) >= 1e-9,
}


def _parse_expr(expr: str) -> tuple[str, float]:
    m = re.match(r"^\s*(>=|<=|==|!=|>|<)\s*([-\d.eE+]+)\s*$", expr or "")
    if not m:
        raise ValueError(f"表达式格式错误: {expr!r}（示例 '>90'）")
    return m.group(1), float(m.group(2))


def detect(algorithm: str, expr: str, points: list[tuple]) -> list[tuple[dict, float]]:
    """输入 [(ts, value)]，返回异常点 [(labels={}, value)]——labels 由调用方补 host/metric。"""
    if not points:
        return []
    if algorithm == "3sigma" or (algorithm == "isolation_forest" and not _sklearn_ok()):
        vals = [v for _, v in points]
        if len(vals) < 5:
            return []
        mu, sg = mean(vals), (stdev(vals) if len(vals) > 1 else 0.0)
        if sg == 0:
            return []
        return [({}, v) for _, v in points if abs(v - mu) > 3 * sg]
    if algorithm == "isolation_forest":
        from sklearn.ensemble import IsolationForest

        vals = [[v] for _, v in points]
        model = IsolationForest(n_estimators=50, contamination=0.05).fit(vals)
        return [({}, points[i][1]) for i, p in enumerate(model.predict(vals)) if p == -1]
    # threshold
    op, thr = _parse_expr(expr)
    fn = _OPS[op]
    return [({}, v) for _, v in points if fn(v, thr)]


def detect_labeled(algorithm: str, expr: str, points: list[tuple[dict, float]]) -> list[tuple[dict, float]]:
    """带标签的检测（轨 B / Prom 专用）：points = [(labels, value)]，返回保留 labels 的异常点。

    与 detect() 的区别：detect() 丢弃 labels（轨 A 由调用方按 host 补齐），
    而 Prom 一个查询可返回多条序列（不同 instance），必须回指原序列标签，否则无法定位主机。
    """
    if not points:
        return []
    if algorithm in ("3sigma",) or (algorithm == "isolation_forest" and not _sklearn_ok()):
        vals = [v for _, v in points]
        if len(vals) < 5:
            return []
        mu, sg = mean(vals), (stdev(vals) if len(vals) > 1 else 0.0)
        if sg == 0:
            return []
        return [(lb, v) for lb, v in points if abs(v - mu) > 3 * sg]
    if algorithm == "isolation_forest":
        from sklearn.ensemble import IsolationForest

        vals = [[v] for _, v in points]
        model = IsolationForest(n_estimators=50, contamination=0.05).fit(vals)
        return [points[i] for i, p in enumerate(model.predict(vals)) if p == -1]
    op, thr = _parse_expr(expr)
    fn = _OPS[op]
    return [(lb, v) for lb, v in points if fn(v, thr)]


def _sklearn_ok() -> bool:
    try:
        import sklearn  # noqa: F401

        return True
    except ImportError:
        return False


def algo_available(algorithm: str) -> str:
    """返回实际生效算法（隔离森林不可用时降级）。"""
    if algorithm == "isolation_forest" and not _sklearn_ok():
        return "3sigma"
    return algorithm
