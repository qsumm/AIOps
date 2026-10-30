"""环形时序存储 + 聚合落库（FR-B4；03 §1.5 collect_store）。

- rings: dict[(ident, metric)] → deque(maxlen=保留窗口/采集间隔)，默认 2h@15s=480 点
- 明细窗口查询走内存；超窗走 metric_agg（1m/5m）
- 聚合任务由 sched.py 每 60s 调用 flush_aggregates()

D154（FR-424）新增**带维度**的序列存储：
- series: dict[(ident, metric, tags_key)] → deque，一条序列带一组标签
  （例：pg.connections.current 带 {server: db01}）
- 维度**可选**：不带维度的点仍走原 `_rings`，行为**逐字节不变**（老规则/老看板零影响）
- 事件身份已含标签（engine/state.py 的 fingerprint 用 labels），
  所以只要把维度带进 labels，同一规则在不同维度上自然产生**独立事件**（各自触发/恢复）
"""
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta

RING_MAXLEN = 480  # 2h @ 15s（保留窗口与采集间隔均可配：环境变量覆盖）

_rings: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=RING_MAXLEN))
_flushed_until: dict[tuple[str, str], datetime] = {}  # 每个 ring 已聚合到的时间点
_lock = threading.Lock()

# D154：带维度的序列（ident, metric, tags_key）→ 采样点
_series: dict[tuple[str, str, str], deque] = defaultdict(lambda: deque(maxlen=RING_MAXLEN))
# D154：带维度序列的落库水位（与 _flushed_until 同义，按 (ident, metric, tags_key)）
_flushed_series_until: dict[tuple[str, str, str], datetime] = {}

# D63 服务发现快照（Agent 每分钟随心跳上报的"监听端口 + 高 CPU 进程"）。
# 只存内存不落库：主机数量级 ≤ 千、单份 ≤ 2KB，重启后下一个上报周期即恢复；
# 不进 DB 是刻意为之——它是"当前状态"不是历史，加列要迁移、加表要清理，收益为负。
_services: dict[str, dict] = {}

# 磁盘分区快照（Agent 随心跳上报的"每个挂载点 df -h 明细"）。同为"当前状态"，
# 走内存不落库（与服务发现一致），供大盘 / 主机详情渲染每个分区。
_disks: dict[str, list] = {}


def set_services(ident: str, data: dict) -> None:
    """存最近一份服务快照。入参来自 Agent 上报——**必须做形状与大小防护**（D64 边界）：
    异常/被改坏的 Agent 可能发超大数组或非列表，直接信会撑内存、也会让详情页渲染卡死。"""
    if not isinstance(data, dict):
        return
    ports = data.get("ports") if isinstance(data.get("ports"), list) else []
    procs = data.get("processes") if isinstance(data.get("processes"), list) else []
    clean_ports = []
    for p in ports[:100]:                       # 端口上限 100（正常主机 <30）
        if isinstance(p, dict) and isinstance(p.get("port"), (int, str)) and str(p["port"]).isdigit():
            clean_ports.append({"port": int(p["port"]),
                                "addr": str(p.get("addr") or "")[:64],
                                "proc": str(p.get("proc") or "")[:64]})
    clean_procs = []
    for p in procs[:50]:                        # 进程上限 50（CPU∪内存 TopN 去重后）
        if isinstance(p, dict) and str(p.get("name") or "").strip():
            try:
                cpu = round(float(p.get("cpu") or 0), 1)
            except (TypeError, ValueError):
                cpu = 0.0
            item = {"name": str(p["name"])[:128], "cpu": cpu}
            # D118（FR-A6）：进程监控扩展字段（PID/用户/内存 RSS%/打开文件数/状态），
            # 全部走 clamp/截断——脏字段丢弃，不让异常 Agent 撑爆快照。
            if p.get("state"):
                item["state"] = str(p["state"])[:16]
            if p.get("user"):
                item["user"] = str(p["user"])[:32]
            try:
                if p.get("pid") is not None:
                    item["pid"] = int(p["pid"])
            except (TypeError, ValueError):
                pass
            for k in ("mem_rss", "open_fds"):
                try:
                    item[k] = max(0, int(float(p.get(k))))
                except (TypeError, ValueError):
                    continue
            try:
                if p.get("mem_percent") is not None:   # 缺键不加（D64 脏元素锁：不凭空造字段）
                    item["mem_percent"] = max(0.0, min(100.0, float(p.get("mem_percent"))))
            except (TypeError, ValueError):
                pass
            clean_procs.append(item)
    _services[ident] = {"ports": clean_ports, "processes": clean_procs,
                        "at": datetime.utcnow().isoformat() + "Z"}


def get_services(ident: str) -> dict:
    return _services.get(ident) or {}


# 磁盘分区字段白名单 + 每字段上限（防异常/被改坏的 Agent 发超大值或非数字）。
# 数值按 int clamp 到非负（字节量）；percent 归一化到 [0, 100]。
_DISK_KEYS = ("device", "fstype", "mount")
_DISK_MAX = 64          # 单主机分区上限（正常 < 20，含 tmpfs/overlay 也不超）
_DISK_FIELDS_MAX = {
    "size": 10 ** 16, "used": 10 ** 16, "free": 10 ** 16,  # bytes，≤ 约 9 PB
}


def set_disks(ident: str, data) -> None:
    """存最近一份磁盘分区快照。入参来自 Agent 上报——必须做形状与大小防护（同 D64）：
    只信 list；每项只取白名单字段；数值 clamp；非数字丢弃；条数封顶。"""
    if not isinstance(data, list):
        return
    clean = []
    for p in data[: _DISK_MAX]:
        if not isinstance(p, dict):
            continue
        item: dict = {}
        for k in _DISK_KEYS:
            v = p.get(k)
            item[k] = str(v or "")[:64] if v else ""
        for k, cap in _DISK_FIELDS_MAX.items():
            v = p.get(k)
            try:
                item[k] = max(0, min(int(float(v)), cap))
            except (TypeError, ValueError):
                continue
        try:
            item["percent"] = max(0.0, min(100.0, float(p.get("percent") or 0)))
        except (TypeError, ValueError):
            continue
        if "mount" in item and item["mount"]:
            clean.append(item)
    _disks[ident] = clean


def get_disks(ident: str) -> list[dict]:
    return _disks.get(ident) or []


def list_metric_names(ident: str, prefix: str = "") -> list:
    """该主机在 ring 中已有数据点的指标名（D118 前端多系列图发现用）。
    只读内存 dict，开销可忽略；聚合表侧由调用方补（老指标 ring 空时）。"""
    with _lock:
        names = {m for (i, m) in _rings if i == ident and m.startswith(prefix)}
        # D154：带维度的序列也算（否则只有中间件指标的机器在 UI 上"看不到指标"）
        names |= {m for (i, m, _k) in _series if i == ident and m.startswith(prefix)}
    return sorted(names)


# ──────────────────── D154：维度（tags）────────────────────
# 维度键规范化：k1=v1;k2=v2（按 key 排序，去掉空值）。
# 为什么用**规范化串**而不是哈希（夜莺用 TagHash=MD5(labels)）：
# 哈希无法按维度范围查询（"找出所有 env=prod 的序列"），规范化串可以前缀匹配，
# 且人能直接看懂（排障时一眼看出这条序列是什么）。
_MAX_TAG_KEYS = 8        # 单条序列的维度数上限（防维度爆炸）
_MAX_TAG_VALUE_LEN = 64  # 单个维度值长度上限


def normalize_tags(tags: dict | None) -> dict:
    """清洗维度：只留 str/int/float/bool，值截断，键数封顶，键名合法化。"""
    if not isinstance(tags, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in list(tags.items())[:_MAX_TAG_KEYS]:
        if not isinstance(k, str) or not k.strip():
            continue
        if v is None or isinstance(v, (dict, list, tuple, set)):
            continue
        s = str(v).strip()
        if not s:
            continue
        out[k.strip()[:_MAX_TAG_VALUE_LEN]] = s[:_MAX_TAG_VALUE_LEN]
    return out


def tags_key(tags: dict | None) -> str:
    """维度 → 规范化键（k1=v1;k2=v2，按 key 排序）。"""
    t = normalize_tags(tags)
    if not t:
        return ""
    return ";".join(f"{k}={t[k]}" for k in sorted(t))


def parse_tags_key(key: str) -> dict:
    """规范化键 → 维度字典（tags_key 的逆运算）。"""
    out: dict[str, str] = {}
    for part in (key or "").split(";"):
        if not part:
            continue
        k, _, v = part.partition("=")
        if k:
            out[k] = v
    return out


def parse_selector(metric: str) -> tuple[str, dict | None]:
    """解析 Prometheus 风格的指标选择器：`name{k=v,k2=v2}`。

    返回 (指标名, 维度字典)：
      - 无花括号 → (name, None)      —— **维度不限**（老行为：一条 series）
      - `name{}`  → (name, {})        —— 该指标的所有带维度序列
      - `name{k=v}` → (name, {"k":"v"}) —— 精确匹配该维度组合

    语法错误不抛异常（规则保存时另有校验），而是按"字面量指标名"处理，
    避免一条写错的选择器把整条规则搞挂。
    """
    m = (metric or "").strip()
    if not m.endswith("}") or "{" not in m:
        return m, None
    name, _, rest = m.partition("{")
    inner = rest[:-1] if rest.endswith("}") else rest
    tags: dict[str, str] = {}
    ok = True
    for part in inner.split(","):
        part = part.strip()
        if not part:
            continue
        k, sep, v = part.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not sep or not k:
            ok = False
            break
        tags[k] = v
    if not ok:
        return m, None      # 语法有问题 → 当普通指标名（查不到数据，不会误命中）
    return name.strip(), tags


def append_point(ident: str, metric: str, ts: datetime, value: float,
                  tags: dict | None = None) -> None:
    """写入一个采样点。带 tags → 进带维度序列；不带 → 进原 ring（老行为不变）。"""
    t = normalize_tags(tags)
    if t:
        with _lock:
            _series[(ident, metric, tags_key(t))].append((ts, value))
        return
    with _lock:
        _rings[(ident, metric)].append((ts, value))


def query_points(ident: str, metric: str, start: datetime, end: datetime,
                 tags: dict | None = None) -> list[tuple[datetime, float]]:
    """窗口内的点。

    tags=None → **老行为**（只查 `_rings`，无维度序列）；
    tags={}   → 该指标**所有**带维度序列的点（按时间合并）；
    tags={k:v}→ 精确匹配该维度组合的点。
    """
    if tags is None:
        with _lock:
            ring = _rings.get((ident, metric))
        if not ring:
            return []
        return [(ts, v) for ts, v in list(ring) if start <= ts <= end]
    with _lock:
        if tags:
            rings = [_series.get((ident, metric, tags_key(tags)))]
        else:
            rings = [v for (i, m, _k), v in _series.items() if i == ident and m == metric]
        pts: list[tuple[datetime, float]] = []
        for r in rings:
            if r:
                pts.extend(list(r))
    return sorted([(ts, v) for ts, v in pts if start <= ts <= end], key=lambda x: x[0])


def _subset_match(series_tags: dict, want: dict) -> bool:
    """序列维度是否包含筛选条件的全部键值（筛选是子集语义）。"""
    return all(str(series_tags.get(k, "")) == str(v) for k, v in want.items())


def query_series(ident: str, metric: str, tags_filter: dict | None,
                 start: datetime, end: datetime) -> list[tuple[dict, list[tuple[datetime, float]]]]:
    """取某指标的**每条带维度序列**的窗口内点。

    返回 [(维度字典, [(ts, value), ...])]，按维度键排序（结果稳定，便于测试）。
    tags_filter 为空 → 该指标的所有带维度序列；非空 → 精确匹配该维度组合。
    """
    out: list[tuple[dict, list[tuple[datetime, float]]]] = []
    with _lock:
        keys = [k for k in _series if k[0] == ident and k[1] == metric]
        if tags_filter:
            # 选择器是**子集筛选**（{state=active} 命中所有含 state=active 的序列），
            # 不是完整键相等——序列的真实键往往带更多维度（datname/usename…）。
            keys = [k for k in keys
                    if _subset_match(parse_tags_key(k[2]), tags_filter)]
        for k in keys:
            r = _series.get(k)
            if not r:
                continue
            pts = [(ts, v) for ts, v in list(r) if start <= ts <= end]
            if pts:
                out.append((parse_tags_key(k[2]), pts))
    return sorted(out, key=lambda x: sorted(x[0].items()))


def list_series(ident: str, metric: str = "") -> list[dict]:
    """该主机的带维度序列清单：[{tags: {...}, points: N}]。

    供"指标查询"页与规则编辑器的维度自动补全用。
    """
    out: list[dict] = []
    with _lock:
        for (i, m, k), r in _series.items():
            if i != ident:
                continue
            if metric and not m.startswith(metric):
                continue
            out.append({"metric": m, "tags": parse_tags_key(k), "points": len(r)})
    return sorted(out, key=lambda x: (x["metric"], sorted(x["tags"].items())))


def series_stats() -> dict:
    """带维度序列的规模统计（看板/自监控用）。"""
    with _lock:
        return {"series": len(_series), "points": sum(len(r) for r in _series.values())}


def has_points_before(ident: str, metric: str, ts: datetime) -> bool:
    """ring 里是否存在**早于 ts** 的点（= ring 是否覆盖到 ts 之前）。

    用途（D15 审计修复）：判断"查询窗口的前段是否超出 ring 覆盖，需不需要聚合表补"。
    为什么不能用"ring 是否满"判断：平台进程重启后 ring 是空的（内存层），
    此时明明只有 1 个点（远未满），但**历史确实在聚合表里**——用"未满"判断会漏补，
    表现为"查 1 天只回重启后的 1 个点"（D15 实测踩到）。
    也不能用"窗口起点早于窗口内最早点"判断：采样点是离散的，窗口内第一个点必然晚于
    窗口起点，这样几乎总会误判需要补（把 1 分钟粒度的聚合行混进 15 秒粒度的完整序列）。
    """
    with _lock:
        r = _rings.get((ident, metric))
        return bool(r) and r[0][0] < ts


def ring_stats() -> dict:
    with _lock:
        return {"series": len(_rings), "points": sum(len(r) for r in _rings.values())}


def cluster_series(metrics: list[str], start: datetime, end: datetime, bucket_s: int = 60) -> list[dict]:
    """集群平均序列（大盘用）：按 metric 汇总所有主机，按 bucket_s 分桶取均值。

    返回 [{"name": metric, "points": [[iso_ts, avg], ...]}]；无数据时返回空列表。
    """
    out: list[dict] = []
    with _lock:
        snapshot = {k: list(v) for k, v in _rings.items()}
    for metric in metrics:
        buckets: dict[datetime, list[float]] = defaultdict(list)
        for (ident, m), points in snapshot.items():
            if m != metric:
                continue
            for ts, v in points:
                if start <= ts <= end:
                    align = int(ts.timestamp() // bucket_s) * bucket_s
                    buckets[datetime.utcfromtimestamp(align)].append(v)
        if not buckets:
            continue
        pts = [[ts.isoformat(), round(sum(vals) / len(vals), 4)] for ts, vals in sorted(buckets.items())]
        out.append({"name": metric, "points": pts})
    return out


async def flush_aggregates(db) -> int:
    """将各 ring 未聚合区间降采样为 1m 桶写入 metric_agg；成功后释放已聚合明细。

    返回写入行数。由 APScheduler 每 60s 调用（02 §5 常驻任务）。
    """
    from sqlalchemy import text

    written = 0
    now = datetime.utcnow()
    cur_minute = now.replace(second=0, microsecond=0)
    with _lock:
        snapshot = {k: list(v) for k, v in _rings.items()}
    for (ident, metric), points in snapshot.items():
        if not points:
            continue
        # 水位默认取"从未聚合"，而不是 points[0][0]（那会永远丢掉该序列的第一个采样点）
        last_flush = _flushed_until.get((ident, metric), datetime.min)
        buckets: dict[datetime, list[float]] = defaultdict(list)
        newest: datetime | None = None
        for ts, v in points:
            # 两个条件缺一不可：
            # ① `ts > last_flush`——水位必须记**原始最大 ts**，不能记桶起点（分钟）。
            #    记桶起点 = 同一分钟的后半段采样点每轮都被判为"未聚合"，反复插入同一个
            #    (ident, metric, ts)。D9 实测：scrape:2/disk.percent 在同一 ts=06:16:00 堆了
            #    8 行重复；这些重复行经 detect 逐点判定 → 一轮刷出 8 条一模一样的告警事件。
            # ② `minute < cur_minute`——只聚合**已闭合**的分钟；当前这一分钟还在收数据，
            #    提前聚合会把同一分钟拆成两次写入，同样是重复行。
            if ts > last_flush and ts.replace(second=0, microsecond=0) < cur_minute:
                buckets[ts.replace(second=0, microsecond=0)].append(v)
                if newest is None or ts > newest:
                    newest = ts
        if not buckets:
            continue
        rows = []
        for minute, vals in sorted(buckets.items()):
            rows.append(
                {
                    "ident": ident,
                    "metric": metric,
                    "granularity": "1m",
                    "ts": minute,
                    "value_avg": sum(vals) / len(vals),
                    "value_min": min(vals),
                    "value_max": max(vals),
                }
            )
        await db.execute(
            text(
                "INSERT INTO metric_agg (ident, metric, granularity, ts, value_avg, value_min, value_max) "
                "VALUES (:ident, :metric, :granularity, :ts, :value_avg, :value_min, :value_max)"
            ),
            rows,
        )
        written += len(rows)
        _flushed_until[(ident, metric)] = newest or last_flush     # 水位＝已聚合样本的原始最大 ts
    await db.commit()
    return written


async def flush_series(db) -> int:
    """把带维度序列（_series）未落库的点按水位写入 metric_series 表（D159 / 缺陷 D158-1）。

    与 flush_aggregates 同款水位语义：
      · 只写 ts > 水位 的点；水位＝已写样本的原始最大 ts（首次 = datetime.min）；
      · 先快照再写、写完一次性推进水位 → 二次调用不产生重复行。
    取舍：_series 是 deque(maxlen=RING_MAXLEN)，两次 flush 之间若单序列新点超过 maxlen，
    最旧的点会被挤出内存而来不及落库——与 _rings 聚合窗口同款（60s 一次 flush 远够）。
    """
    from sqlalchemy import text
    import json as _json

    with _lock:
        snapshot = {k: list(v) for k, v in _series.items()}
    rows: list[dict] = []
    watermarks: dict[tuple[str, str, str], datetime] = {}
    for (ident, metric, tk), points in snapshot.items():
        if not points:
            continue
        last_flush = _flushed_series_until.get((ident, metric, tk), datetime.min)
        tags_json = _json.dumps(parse_tags_key(tk), ensure_ascii=False)
        newest: datetime | None = None
        for ts, v in points:
            if ts > last_flush:
                rows.append({"ident": ident, "metric": metric, "tags_key": tk,
                             "tags": tags_json, "value": v, "ts": ts})
                if newest is None or ts > newest:
                    newest = ts
        if newest:
            watermarks[(ident, metric, tk)] = newest
    if not rows:
        return 0
    await db.execute(
        text(
            "INSERT INTO metric_series (ident, metric, tags_key, tags, value, ts) "
            "VALUES (:ident, :metric, :tags_key, :tags, :value, :ts)"
        ),
        rows,
    )
    await db.commit()
    with _lock:
        _flushed_series_until.update(watermarks)
    return len(rows)
