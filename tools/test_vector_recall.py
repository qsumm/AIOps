#!/usr/bin/env python3
"""向量检索准确性实测（用户问「目前向量检索准吗」）。

测什么：
  用**人话**描述现象提问（刻意避开案例卡原文用词），看能否把对的卡片排第一；
  再用「库里没有对应案例」的查询检查区分度（距离应明显更大）。

判定：
  · 命中 = 期望卡片（或语义等价的卡片）排第 1
  · 对照 = 无对应案例时，看 Top1 距离是否与「有命中」时拉开差距

用法：python tools/test_vector_recall.py --base http://127.0.0.1:8849 --user admin --pass admin123
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request

# (查询, 可接受的 Top1 卡片 id 集合, 说明)  —— 空集合表示"库里没有对应案例"（对照组）
CASES: list[tuple[str, set[int], str]] = [
    # 磁盘类（案例：磁盘用量单调上升 / 慢日志未轮转；测试库里另有「磁盘写满」卡）
    ("日志把根分区撑满了，怎么处理", {21, 2}, "磁盘类·换词问法"),
    ("服务器硬盘空间一直往上涨", {21, 2}, "磁盘类·口语说法"),
    ("磁盘水位持续升高没有回落", {21, 2}, "磁盘类·术语变体"),
    # CPU 类（案例：整点 CPU 冲高、定时任务集中执行）
    ("每天整点系统就卡一阵", {22}, "CPU类·换词问法"),
    ("CPU 周期性飙升又自己恢复", {22}, "CPU类·现象描述"),
    ("定时任务把 CPU 抢满了", {22}, "CPU类·指向动作"),
    # 连接类（案例：连接未归还、连接池打满）
    ("接口大面积超时，重启能缓解但过会儿又复发", {23, 137}, "连接类·换词问法"),
    ("连接池被占满了", {23, 137}, "连接类·直接说根因"),
    ("应用侧连接泄漏导致超时", {23, 137}, "连接类·术语"),
    # 对照组：库里没有对应案例
    ("HTTPS 证书快到期了", set(), "对照·无对应案例"),
    ("主机突然离线联系不上", set(), "对照·无对应案例"),
    ("内存占用缓慢泄漏", set(), "对照·无对应案例"),
]


def recall(base: str, tok: str, q: str, k: int = 3) -> dict:
    url = base.rstrip("/") + "/api/ops/case-cards/recall?" + urllib.parse.urlencode({"q": q, "top_k": k})
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["data"]


def login(base: str, user: str, pwd: str) -> str:
    body = json.dumps({"username": user, "password": pwd}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["data"]["access_token"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8849")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--pass", dest="pwd", default="admin123")
    args = ap.parse_args()

    tok = login(args.base, args.user, args.pwd)
    ok = total = 0
    hit_dists: list[float] = []
    ctrl_dists: list[float] = []

    print("=" * 78)
    for q, accept, note in CASES:
        d = recall(args.base, tok, q)
        items = d.get("items") or []
        mode = d.get("mode")
        if not items:
            print("[%s] %s\n    → 无结果（mode=%s）" % (note, q, mode))
            continue
        top = items[0]
        tid, dist = top.get("id"), float(top.get("distance") or 0)
        others = ", ".join("#%s:%.3f" % (i.get("id"), float(i.get("distance") or 0)) for i in items[1:])

        if accept:
            total += 1
            hit = tid in accept
            ok += 1 if hit else 0
            hit_dists.append(dist)
            print("%s [%s] %s" % ("✓" if hit else "✗", note, q))
            print("    期望 %s → 实得 #%s（dist=%.4f） [%s]" % (sorted(accept), tid, dist, mode))
            if not hit:
                print("    实得内容：%s" % str(top.get("symptom"))[:46])
        else:
            ctrl_dists.append(dist)
            print("·  [%s] %s" % (note, q))
            print("    最相似 #%s dist=%.4f [%s]（对照组：距离应偏大）" % (tid, dist, mode))
        if others:
            print("    其余：%s" % others)

    print("=" * 78)
    if total:
        print("Top-1 命中率：%d/%d = %.0f%%" % (ok, total, ok / total * 100))
    if hit_dists and ctrl_dists:
        print("距离分布：命中组 %.4f ~ %.4f（均值 %.4f）｜对照组 %.4f ~ %.4f（均值 %.4f）"
              % (min(hit_dists), max(hit_dists), sum(hit_dists) / len(hit_dists),
                 min(ctrl_dists), max(ctrl_dists), sum(ctrl_dists) / len(ctrl_dists)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
