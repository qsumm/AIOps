"""D163：补 node_exporter → 平台指标 映射表（用户要求"补一套 node_* → 平台指标 映射表"）。

**现场**：node_exporter 是最主流的主机 exporter，但平台此前**几乎拿不到它的指标**：
  ① `map_to_platform_metrics` 里 `name.startswith("node_")` 的指标被**直接跳过**
     （"其余按 common 前缀直接透传"那段把 node_ 整个排除）；
  ② `GAUGE_MAP` 只有 7 项，且走 `_first()` —— **只取第一条序列**，
     node_network_receive_bytes_total 有 eth0/lo 多条，只剩一条；
  ③ 结论：装了 node_exporter 的用户，平台只能拿到 CPU/内存/磁盘三个使用率，
     **按设备的磁盘 IO、按网卡的流量、TCP 连接数、文件句柄、负载全部拿不到**。

修：新增 `NODE_EXPORTER_MAP`（清单依据 **node_exporter v1.8.2 靶机真机 225 个指标名**
逐条核对），走**带维度路径**逐序列入库；并把网络域的 `device` 标签归一为 `interface`
（Categraf 的 net 插件用 device 表示网卡，node_exporter 用 interface）。
"""
from __future__ import annotations

from app.services.prom_scrape import (NODE_EXPORTER_MAP, _parse_exposition,
                                      _keep_labels, map_to_platform_metrics,
                                      map_to_platform_series)

# 靶机 node_exporter v1.8.2 的真实指标（节选，覆盖各域）
NODE_SAMPLE = """# HELP node_network_receive_bytes_total Network device statistic bytes received
# TYPE node_network_receive_bytes_total counter
node_network_receive_bytes_total{device="eth0"} 2174830203
node_network_receive_bytes_total{device="lo"} 151112795
# HELP node_disk_read_bytes_total Disk bytes read
# TYPE node_disk_read_bytes_total counter
node_disk_read_bytes_total{device="vda"} 1849928192
node_disk_read_bytes_total{device="sr0"} 191727616
# HELP node_filesystem_size_bytes Filesystem size in bytes
# TYPE node_filesystem_size_bytes gauge
node_filesystem_size_bytes{device="/dev/vda1",fstype="ext4",mountpoint="/"} 52709240832
node_filesystem_size_bytes{device="tmpfs",fstype="tmpfs",mountpoint="/run"} 1047805952
# HELP node_netstat_Tcp_CurrEstab Number of current established TCP connections
# TYPE node_netstat_Tcp_CurrEstab gauge
node_netstat_Tcp_CurrEstab 47
# HELP node_filefd_allocated File descriptor statistics
# TYPE node_filefd_allocated gauge
node_filefd_allocated 1024
node_filefd_maximum 1048576
# HELP node_procs_running Number of processes in runnable state
# TYPE node_procs_running gauge
node_procs_running 3
# HELP node_boot_time_seconds Node boot time
# TYPE node_boot_time_seconds gauge
node_boot_time_seconds 1792000000
# HELP node_load1 1m load average
# TYPE node_load1 gauge
node_load1 0.42
node_load5 0.38
node_load15 0.35
# HELP node_memory_MemTotal_bytes Memory information field MemTotal_bytes
# TYPE node_memory_MemTotal_bytes gauge
node_memory_MemTotal_bytes 2009235456
node_memory_MemAvailable_bytes 1470000000
# HELP node_vmstat_pgfault Page faults
# TYPE node_vmstat_pgfault gauge
node_vmstat_pgfault 123456
node_vmstat_pgmajfault 789
# HELP node_scrape_collector_success Whether the collector succeeded
# TYPE node_scrape_collector_success gauge
node_scrape_collector_success{collector="cpu"} 1
node_scrape_collector_success{collector="diskstats"} 1
"""


def test_node_exporter_map_covers_all_domains():
    """映射表必须覆盖主机层的关键域，缺一项就是一类指标拿不到。"""
    for raw in ("node_network_receive_bytes_total", "node_disk_read_bytes_total",
                "node_filesystem_size_bytes", "node_netstat_Tcp_CurrEstab",
                "node_filefd_allocated", "node_procs_running", "node_load1",
                "node_memory_MemTotal_bytes", "node_vmstat_pgfault",
                "node_boot_time_seconds", "node_scrape_collector_success"):
        assert raw in NODE_EXPORTER_MAP, f"映射表缺 {raw}"
        assert NODE_EXPORTER_MAP[raw].count(".") >= 1, f"{raw} 的平台名应带域前缀"


def test_node_series_keep_dimensions():
    """D163 核心：多序列指标必须**逐条保留维度**，不能被压成一条。

    这是此前 GAUGE_MAP + `_first()` 的老毛病：eth0/lo、vda/sr0 只剩第一条。
    """
    parsed = _parse_exposition(NODE_SAMPLE)
    series = map_to_platform_series(parsed)

    net = [t for m, t, _ in series if m == "net.bytes_recv"]
    assert len(net) == 2, f"网卡维度应保留 2 条（eth0/lo），实际 {len(net)}"
    assert {t.get("interface") for t in net} == {"eth0", "lo"}, net

    dio = [t for m, t, _ in series if m == "disk.io.read.bytes"]
    assert len(dio) == 2, f"磁盘设备维度应保留 2 条（vda/sr0），实际 {len(dio)}"
    assert {t.get("device") for t in dio} == {"vda", "sr0"}, dio

    fs = [t for m, t, _ in series if m == "disk.size.bytes"]
    assert len(fs) == 2, f"挂载点维度应保留 2 条，实际 {len(fs)}"
    assert {t.get("mountpoint") for t in fs} == {"/", "/run"}, fs
    # fstype 是噪声标签（结构性），不该留在维度里
    assert all("fstype" not in t for t in fs), fs


def test_node_flat_view_has_mapped_names():
    """扁平视图（测试按钮预览）也要能看到 node_* 映射后的平台名。"""
    flat = map_to_platform_metrics(_parse_exposition(NODE_SAMPLE))
    for name in ("net.bytes_recv", "net.tcp.established", "process.fd.allocated",
                 "load.1", "load.5", "load.15", "vm.pgfault", "vm.pgmajfault",
                 "system.boot_time_seconds", "mem.total.bytes", "mem.available.bytes"):
        assert name in flat, f"扁平视图缺 {name}"


def test_network_labels_normalized_to_interface():
    """网络域的 device 标签要归一成 interface（与 node_exporter 一致）。

    否则用户写 `net.bytes_recv{interface=eth0}` 在 Categraf 源上永远不命中。
    但**磁盘**的 device 是磁盘设备名，不能一起改。
    """
    categraf_net = {"device": "eth0", "agent_hostname": "host1"}
    assert _keep_labels(categraf_net, None, "net.bytes_recv").get("interface") == "eth0"
    assert "device" not in _keep_labels(categraf_net, None, "net.bytes_recv")

    disk_tags = _keep_labels({"device": "vda"}, None, "disk.io.read.bytes")
    assert disk_tags.get("device") == "vda", "磁盘的 device 不得被改成 interface"
    assert "interface" not in disk_tags


def test_counter_metrics_keep_total_suffix():
    """counter 类指标名带 `_total`（node_exporter 惯例）——映射表必须认这个真名。

    坑：官方解析器会给 counter 自动补 `_total`（`node_vmstat_pgfault` →
    `node_vmstat_pgfault_total`），映射表若只写不带后缀的名字就永远匹配不上。
    真机的 vmstat 是 gauge（无后缀），但 counter 类（disk/network/netstat）
    一律带 `_total`——两边都要覆盖。
    """
    parsed = _parse_exposition(
        "# TYPE node_x counter\nnode_x 1\n")
    assert "node_x_total" in parsed, "解析器应给 counter 补 _total"
    # 映射表里带 _total 的项（真机 disk/network/netstat 全是这个形态）
    assert "node_disk_read_bytes_total" in NODE_EXPORTER_MAP
    assert "node_network_receive_bytes_total" in NODE_EXPORTER_MAP
    assert "node_netstat_Tcp_CurrEstab" in NODE_EXPORTER_MAP
    # 真机 vmstat 是 gauge（无后缀）——映射表也按真名写
    assert "node_vmstat_pgfault" in NODE_EXPORTER_MAP


def test_user_metric_map_overrides_builtin():
    """用户自定义映射覆盖内置表（node_* 也不例外）。"""
    parsed = _parse_exposition(NODE_SAMPLE)
    series = map_to_platform_series(parsed, user_map={"node_load1": "my.load"})
    assert any(m == "my.load" for m, _, _ in series), "用户映射未生效"
    assert not any(m == "load.1" for m, _, _ in series), "被覆盖的内置项不应再出现"
