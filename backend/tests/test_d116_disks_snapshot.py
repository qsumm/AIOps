"""磁盘分区快照（df -h 明细）的形状防护 + 存取的回归测试（对应 agent.collect_disks → ring.set_disks 链路）。

与 D63 services 快照同一套内存模式：只存最近一份、只信白名单字段、数值 clamp、非数字丢弃、
条数封顶。这里锁死 set_disks/get_disks 的防护行为，防止异常 Agent 上报撑内存或脏数据进界面。
"""
from app.store import ring


def _clear():
    ring._disks.clear()


def test_set_disks_ignores_non_list():
    _clear()
    ring.set_disks("h1", "not-a-list")
    ring.set_disks("h1", {"mount": "/"})
    ring.set_disks("h1", 42)
    assert ring.get_disks("h1") == []


def test_set_disks_whitelist_and_clamp():
    _clear()
    ring.set_disks("h1", [
        {"device": "/dev/vda3", "fstype": "ext4", "size": 40 * 1024 ** 3,
         "used": 31 * 1024 ** 3, "free": 7 * 1024 ** 3, "percent": 83.0, "mount": "/", "extra": "drop-me"},
        # 异常字段：负值 clamp、非数字 free/used 丢弃、percent 超界归一
        {"device": 9, "fstype": 1, "size": -5, "used": None, "free": "x", "percent": 999.0, "mount": "/bad"},
    ])
    parts = ring.get_disks("h1")
    assert len(parts) == 2
    p0 = parts[0]
    assert p0["mount"] == "/" and p0["device"] == "/dev/vda3" and p0["fstype"] == "ext4"
    assert p0["size"] == 40 * 1024 ** 3 and p0["percent"] == 83.0
    assert "extra" not in p0  # 非白名单字段不保留
    p1 = parts[1]
    assert p1["size"] == 0        # 负值 → 0
    assert p1["percent"] == 100.0  # 越界 → 100
    assert "free" not in p1       # 非数字 free → 丢弃该键
    assert "used" not in p1       # None used → 丢弃


def test_set_disks_cap_and_last_wins():
    _clear()
    many = [{"device": f"/dev/n{i}", "fstype": "xfs", "size": 1, "used": 0, "free": 1,
             "percent": 1.0, "mount": f"/m{i}"} for i in range(80)]
    ring.set_disks("h1", many)
    assert len(ring.get_disks("h1")) <= 64
    # 新快照覆盖旧快照（内存只保最近一份）
    ring.set_disks("h1", [{"device": "/dev/new", "fstype": "ext4", "size": 2,
                           "used": 1, "free": 1, "percent": 50.0, "mount": "/"}])
    parts = ring.get_disks("h1")
    assert len(parts) == 1 and parts[0]["device"] == "/dev/new"