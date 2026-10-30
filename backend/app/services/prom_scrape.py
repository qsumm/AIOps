"""Prometheus / node_exporter **直采**（FR-B3+，V1.5 的"架构预留"落地）。

与轨 B（查询代理）的区别：
- 轨 B：平台**查** Prometheus 的 API（`/api/v1/query_range`），数据不落本地库；
- 轨 B+（本模块）：平台**拉** exporter 的 `/metrics` 文本，用官方解析器解析后，
  按下面的映射表转成平台自己的指标名，写入内存环形 + 聚合落库（与内置 Agent 同一条下游链路）。

价值：**已有 node_exporter 的用户零改造接入**——不用装 Agent，也能在主机列表看到这台机器、
在主机详情出图、对它的 CPU/内存/磁盘建告警规则。

设计取舍：
- 只做"（少量）常用 node_exporter 指标 → 平台指标"的映射，不做全量指标透传：
  全量会让环形缓冲和聚合表瞬间被上千条无用序列撑爆；要全量请用轨 B 直接查 Prom。
- 抓取失败只记状态，不影响其它数据源与告警链路（与 Prom 查询侧的"不可达即跳过本轮"同思路）。
- 合成主机 ident = `scrape:<datasource_id>`，`hosts.name` 用数据源名 + `tags={"source":"scrape"}`，
  便于在 UI 上区分"Agent 采的"与"直接从 exporter 拉的"。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx

from app.db.session import SessionLocal
from app.models.host import Host
from app.models.prom import PromDatasource
from app.store import ring

log = logging.getLogger("prom_scrape")

SCRAPE_IDENT = "scrape:{did}"

# node_exporter 指标 → 平台指标（线性映射：value = raw * scale + offset，或自定义函数）
# 说明：CPU 使用率要用"各 mode 的非 idle 占比"，需要聚合同一 metric 的多条序列，
# 故单独处理（见 _cpu_percent）。
CPU_MODES = ("user", "system", "nice", "iowait", "irq", "softirq", "steal")

# ── D163：node_exporter 主机层映射表（FR-422 · 用户要求"补一套 node_* → 平台指标 映射表"）
#
# **为什么需要这张表**：此前 node_exporter 的指标**几乎全部拿不到**——
#   ① `map_to_platform_metrics` 里 `name.startswith("node_")` 的指标被**直接跳过**
#      （"其余按 common 前缀直接透传"那一段把 node_ 整个排除），只透传非 node_ 的；
#   ② `GAUGE_MAP` 只有 7 项，且走 `_first()` —— **只取第一条序列**，
#      于是 node_network_receive_bytes_total 有 eth0/lo 多条序列，只剩一条；
#   ③ 结论：装了 node_exporter 的用户，平台只能拿到 CPU/内存/磁盘三个使用率，
#      **磁盘按设备、网络按网卡、TCP 连接数、文件句柄、负载、磁盘 IO 全部拿不到**。
#
# 本表把 node_exporter 的主机层指标映射成平台指标名，**并逐序列保留维度**
# （device / mountpoint / interface / protocol / state …）——
# 这样「每块盘各算各的使用率」「每张网卡各算各的流量」才能成立。
#
# 命名遵循平台既有约定：`域.指标名`（如 disk.io.read.bytes / net.tcp.established）。
# 清单依据：**node_exporter v1.8.2 在靶机真机 /metrics 的 225 个指标名**逐条核对，
# 不是凭记忆写的（见 docs/04-交付与运营/缺陷台账.md §1.26 与测试报告）。
NODE_EXPORTER_MAP: dict[str, str] = {
    # —— 负载 ——
    "node_load1": "load.1",
    "node_load5": "load.5",
    "node_load15": "load.15",
    # —— 内存（字节）——
    "node_memory_MemTotal_bytes": "mem.total.bytes",
    "node_memory_MemAvailable_bytes": "mem.available.bytes",
    "node_memory_MemFree_bytes": "mem.free.bytes",
    "node_memory_Cached_bytes": "mem.cached.bytes",
    "node_memory_Buffers_bytes": "mem.buffers.bytes",
    "node_memory_SwapTotal_bytes": "mem.swap.total.bytes",
    "node_memory_SwapFree_bytes": "mem.swap.free.bytes",
    "node_memory_SwapCached_bytes": "mem.swap.cached.bytes",
    "node_memory_Shmem_bytes": "mem.shared.bytes",
    "node_memory_Slab_bytes": "mem.slab.bytes",
    "node_memory_PageTables_bytes": "mem.page_tables.bytes",
    "node_memory_Active_bytes": "mem.active.bytes",
    "node_memory_Inactive_bytes": "mem.inactive.bytes",
    "node_memory_Unevictable_bytes": "mem.unevictable.bytes",
    "node_memory_Mlocked_bytes": "mem.locked.bytes",
    "node_memory_Dirty_bytes": "mem.dirty.bytes",
    "node_memory_Writeback_bytes": "mem.writeback.bytes",
    "node_memory_Committed_AS_bytes": "mem.committed.bytes",
    "node_memory_CommitLimit_bytes": "mem.commit_limit.bytes",
    "node_memory_HugePages_Total": "mem.hugepages.total",
    "node_memory_HugePages_Free": "mem.hugepages.free",
    "node_memory_Hugepagesize_bytes": "mem.hugepages.size.bytes",
    # —— 文件系统（按 mountpoint/device，**逐序列**）——
    "node_filesystem_size_bytes": "disk.size.bytes",
    "node_filesystem_avail_bytes": "disk.avail.bytes",
    "node_filesystem_free_bytes": "disk.free.bytes",
    "node_filesystem_files": "disk.inodes.total",
    "node_filesystem_files_free": "disk.inodes.free",
    "node_filesystem_readonly": "disk.readonly",
    "node_filesystem_device_error": "disk.device_error",
    # —— 磁盘 IO（按 device，**逐序列**）——
    "node_disk_read_bytes_total": "disk.io.read.bytes",
    "node_disk_written_bytes_total": "disk.io.write.bytes",
    "node_disk_reads_completed_total": "disk.io.reads.completed",
    "node_disk_writes_completed_total": "disk.io.writes.completed",
    "node_disk_read_time_seconds_total": "disk.io.read.time_seconds",
    "node_disk_write_time_seconds_total": "disk.io.write.time_seconds",
    "node_disk_io_time_seconds_total": "disk.io.busy.time_seconds",
    # —— 网络（按 interface，**逐序列**）——
    "node_network_receive_bytes_total": "net.bytes_recv",
    "node_network_transmit_bytes_total": "net.bytes_sent",
    "node_network_receive_packets_total": "net.packets_recv",
    "node_network_transmit_packets_total": "net.packets_sent",
    "node_network_receive_errs_total": "net.errors_recv",
    "node_network_transmit_errs_total": "net.errors_sent",
    "node_network_receive_drop_total": "net.dropped_recv",
    "node_network_transmit_drop_total": "net.dropped_sent",
    "node_network_mtu_bytes": "net.mtu.bytes",
    "node_network_up": "net.up",
    # —— TCP/UDP（按 protocol 维度）——
    "node_netstat_Tcp_CurrEstab": "net.tcp.established",
    "node_netstat_Tcp_ActiveOpens": "net.tcp.active_opens",
    "node_netstat_Tcp_PassiveOpens": "net.tcp.passive_opens",
    "node_netstat_Tcp_InSegs": "net.tcp.in_segments",
    "node_netstat_Tcp_OutSegs": "net.tcp.out_segments",
    "node_netstat_Tcp_RetransSegs": "net.tcp.retrans_segments",
    "node_netstat_Tcp_InErrs": "net.tcp.in_errors",
    "node_netstat_Tcp_OutRsts": "net.tcp.out_resets",
    "node_netstat_TcpExt_ListenOverflows": "net.tcp.listen_overflows",
    "node_netstat_TcpExt_ListenDrops": "net.tcp.listen_drops",
    "node_netstat_TcpExt_TCPSynRetrans": "net.tcp.syn_retrans",
    "node_netstat_Udp_InDatagrams": "net.udp.in_datagrams",
    "node_netstat_Udp_OutDatagrams": "net.udp.out_datagrams",
    "node_netstat_Udp_InErrors": "net.udp.in_errors",
    # —— socket / 文件句柄 ——
    "node_sockstat_sockets_used": "net.sockets.used",
    "node_sockstat_TCP_inuse": "net.sockstat.tcp.inuse",
    "node_sockstat_TCP_tw": "net.sockstat.tcp.time_wait",
    "node_sockstat_TCP_alloc": "net.sockstat.tcp.alloc",
    "node_sockstat_TCP_mem": "net.sockstat.tcp.mem",
    "node_sockstat_UDP_inuse": "net.sockstat.udp.inuse",
    "node_sockstat_RAW_inuse": "net.sockstat.raw.inuse",
    "node_filefd_allocated": "process.fd.allocated",
    "node_filefd_maximum": "process.fd.maximum",
    # —— 进程 / 调度 ——
    "node_procs_running": "process.procs_running",
    "node_procs_blocked": "process.procs_blocked",
    "node_forks_total": "process.forks.total",
    "node_context_switches_total": "process.context_switches.total",
    "node_intr_total": "process.interrupts.total",
    # —— 虚拟内存 ——
    "node_vmstat_pgfault": "vm.pgfault",
    "node_vmstat_pgmajfault": "vm.pgmajfault",
    "node_vmstat_pswpin": "vm.pswpin",
    "node_vmstat_pswpout": "vm.pswpout",
    "node_vmstat_pgpgin": "vm.pgpgin",
    "node_vmstat_pgpgout": "vm.pgpgout",
    # —— 系统 / 时间 ——
    "node_boot_time_seconds": "system.boot_time_seconds",
    "node_time_seconds": "system.time_seconds",
    "node_timex_offset_seconds": "system.timex.offset_seconds",
    "node_timex_maxerror_seconds": "system.timex.maxerror_seconds",
    "node_entropy_available_bits": "system.entropy.available_bits",
    "node_scrape_collector_success": "exporter.collector.success",
    "node_scrape_collector_duration_seconds": "exporter.collector.duration_seconds",
    # —— CPU 细分（node_cpu_seconds_total 按 mode 聚合；单 mode 值见 cpu.*）——
    "node_cpu_seconds_total": "cpu.seconds_total",
    "node_cpu_guest_seconds_total": "cpu.guest_seconds_total",
    # —— 内存细分（补齐常用项；未列出的可走指标白名单）——
    "node_memory_Active_anon_bytes": "mem.active_anon.bytes",
    "node_memory_Active_file_bytes": "mem.active_file.bytes",
    "node_memory_AnonPages_bytes": "mem.anon.bytes",
    "node_memory_AnonHugePages_bytes": "mem.anon_hugepages.bytes",
    "node_memory_Inactive_anon_bytes": "mem.inactive_anon.bytes",
    "node_memory_Inactive_file_bytes": "mem.inactive_file.bytes",
    "node_memory_SReclaimable_bytes": "mem.sreclaimable.bytes",
    "node_memory_SUnreclaim_bytes": "mem.sunreclaim.bytes",
    "node_memory_Mapped_bytes": "mem.mapped.bytes",
    "node_memory_KernelStack_bytes": "mem.kernel_stack.bytes",
    "node_memory_Percpu_bytes": "mem.percpu.bytes",
    "node_memory_VmallocTotal_bytes": "mem.vmalloc.total.bytes",
    "node_memory_VmallocUsed_bytes": "mem.vmalloc.used.bytes",
    "node_memory_VmallocChunk_bytes": "mem.vmalloc.chunk.bytes",
    "node_memory_HardwareCorrupted_bytes": "mem.hardware_corrupted.bytes",
    "node_memory_HugePages_Rsvd": "mem.hugepages.reserved",
    "node_memory_HugePages_Surp": "mem.hugepages.surplus",
    "node_memory_WritebackTmp_bytes": "mem.writeback_tmp.bytes",
    "node_memory_CmaTotal_bytes": "mem.cma.total.bytes",
    "node_memory_CmaFree_bytes": "mem.cma.free.bytes",
    # —— 磁盘补充 ——
    "node_disk_io_now": "disk.io.busy.now",
    "node_disk_io_time_weighted_seconds_total": "disk.io.weighted.time_seconds",
    "node_disk_reads_merged_total": "disk.io.reads.merged",
    "node_disk_writes_merged_total": "disk.io.writes.merged",
    # —— 网络补充（按接口的丢包/错误细分）——
    "node_network_receive_multicast_total": "net.multicast_recv",
    "node_network_transmit_multicast_total": "net.multicast_sent",
    "node_network_receive_broadcast_total": "net.broadcast_recv",
    "node_network_transmit_broadcast_total": "net.broadcast_sent",
    "node_network_carrier_changes_total": "net.carrier_changes",
    "node_network_transmit_colls_total": "net.collisions_sent",
    "node_network_receive_fifo_total": "net.fifo_recv",
    "node_network_transmit_fifo_total": "net.fifo_sent",
    "node_network_receive_frame_total": "net.frame_recv",
    "node_network_receive_compressed_total": "net.compressed_recv",
    "node_network_transmit_compressed_total": "net.compressed_sent",
    "node_network_receive_nohandler_total": "net.nohandler_recv",
    # —— ICMP / IP ——
    "node_netstat_Icmp_InMsgs": "net.icmp.in_msgs",
    "node_netstat_Icmp_OutMsgs": "net.icmp.out_msgs",
    "node_netstat_Icmp_InErrors": "net.icmp.in_errors",
    "node_netstat_Icmp6_InMsgs": "net.icmp6.in_msgs",
    "node_netstat_Icmp6_OutMsgs": "net.icmp6.out_msgs",
    "node_netstat_Icmp6_InErrors": "net.icmp6.in_errors",
    "node_netstat_Ip_Forwarding": "net.ip.forwarding",
    "node_netstat_IpExt_InOctets": "net.ip.in_octets",
    "node_netstat_IpExt_OutOctets": "net.ip.out_octets",
    # —— TCP 扩展 ——
    "node_netstat_TcpExt_TCPOFOQueue": "net.tcp.ofo_queue",
    "node_netstat_TcpExt_TCPTimeouts": "net.tcp.timeouts",
    "node_netstat_TcpExt_SyncookiesSent": "net.tcp.syncookies_sent",
    "node_netstat_TcpExt_SyncookiesRecv": "net.tcp.syncookies_recv",
    "node_netstat_TcpExt_SyncookiesFailed": "net.tcp.syncookies_failed",
    # —— socket 内存 ——
    "node_sockstat_TCP_mem_bytes": "net.sockstat.tcp.mem_bytes",
    "node_sockstat_UDP_mem_bytes": "net.sockstat.udp.mem_bytes",
    "node_sockstat_UDP_mem": "net.sockstat.udp.mem",
    "node_sockstat_TCP_orphan": "net.sockstat.tcp.orphan",
    "node_sockstat_FRAG_inuse": "net.sockstat.frag.inuse",
    "node_sockstat_FRAG_memory": "net.sockstat.frag.memory",
    # —— softnet（内核网络栈丢包）——
    "node_softnet_dropped_total": "net.softnet.dropped",
    "node_softnet_processed_total": "net.softnet.processed",
    "node_softnet_received_rps_total": "net.softnet.received_rps",
    "node_softnet_times_squeezed_total": "net.softnet.times_squeezed",
    "node_softnet_backlog_len": "net.softnet.backlog_len",
    "node_softnet_cpu_collision_total": "net.softnet.cpu_collision",
    # —— 调度器 ——
    "node_schedstat_running_seconds_total": "sched.running_seconds",
    "node_schedstat_waiting_seconds_total": "sched.waiting_seconds",
    "node_schedstat_timeslices_total": "sched.timeslices",
    # —— 时间同步 ——
    "node_timex_offset_seconds": "system.timex.offset_seconds",
    "node_timex_maxerror_seconds": "system.timex.maxerror_seconds",
    "node_timex_estimated_error_seconds": "system.timex.estimated_error_seconds",
    "node_timex_pps_error_seconds": "system.timex.pps_error_seconds",
    "node_timex_pps_jitter_seconds": "system.timex.pps_jitter_seconds",
    "node_timex_pps_frequency_hertz": "system.timex.pps_frequency_hertz",
    "node_timex_pps_stability_hertz": "system.timex.pps_stability_hertz",
    "node_timex_sync_status": "system.timex.sync_status",
    "node_timex_status": "system.timex.status",
    "node_time_zone_offset_seconds": "system.time_zone.offset_seconds",
    "node_time_seconds": "system.time_seconds",
    # —— 其他 ——
    "node_arp_entries": "net.arp.entries",
    "node_udp_queues": "net.udp.queues",
    "node_entropy_available_bits": "system.entropy.available_bits",
    "node_entropy_pool_size_bits": "system.entropy.pool_size_bits",
    "node_selinux_enabled": "system.selinux_enabled",
    "node_dmi_info": "system.dmi_info",
    "node_os_version": "system.os_version",
    "node_memory_Bounce_bytes": "mem.bounce.bytes",
    "node_memory_NFS_Unstable_bytes": "mem.nfs_unstable.bytes",
    "node_netstat_Udp_RcvbufErrors": "net.udp.rcvbuf_errors",
    "node_netstat_Udp_SndbufErrors": "net.udp.sndbuf_errors",
    "node_netstat_Udp_NoPorts": "net.udp.no_ports",
    "node_network_carrier": "net.carrier",
    "node_network_dormant": "net.dormant",
    "node_network_transmit_carrier_total": "net.carrier_errors_sent",
    "node_network_transmit_queue_length": "net.queue_length",
    "node_softnet_flow_limit_count_total": "net.softnet.flow_limit",
    "node_timex_pps_error_total": "system.timex.pps_error_total",
    "node_timex_pps_jitter_total": "system.timex.pps_jitter_total",
    "node_timex_pps_shift_seconds": "system.timex.pps_shift_seconds",
    "node_timex_tai_offset_seconds": "system.timex.tai_offset_seconds",
    "node_timex_tick_seconds": "system.timex.tick_seconds",
    "node_timex_pps_stability_exceeded_total": "system.timex.pps_stability_exceeded",
    "node_timex_pps_calibration_total": "system.timex.pps_calibration_total",
    "node_cooling_device_cur_state": "system.cooling.cur_state",
    "node_cooling_device_max_state": "system.cooling.max_state",
}

GAUGE_MAP = {
    "node_load1": ("load.1", 1.0),
    "node_memory_MemAvailable_bytes": ("mem.available.bytes", 1.0),
    "node_memory_MemTotal_bytes": ("mem.total.bytes", 1.0),
    "node_filesystem_avail_bytes": ("disk.avail.bytes", 1.0),
    "node_filesystem_size_bytes": ("disk.size.bytes", 1.0),
    "node_network_receive_bytes_total": ("net.bytes_recv", 1.0),
    "node_network_transmit_bytes_total": ("net.bytes_sent", 1.0),
}


def _parse_exposition(text: str) -> dict[str, list[tuple[dict, float]]]:
    """用官方解析器解析 /metrics 文本 → {metric_name: [(labels, value), ...]}。

    只取 samples；解析失败抛 ValueError（由调用方记状态，不影响其它数据源）。
    """
    from prometheus_client.parser import text_string_to_metric_families

    out: dict[str, list[tuple[dict, float]]] = {}
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            out.setdefault(s.name, []).append((dict(s.labels or {}), float(s.value)))
    return out


def map_to_platform_metrics(parsed: dict[str, list[tuple[dict, float]]]) -> dict[str, float]:
    """把 exporter 指标映射成平台指标名（返回 {平台指标: 值}）。"""
    out: dict[str, float] = {}

    # CPU 使用率：100 * (1 - idle_delta / total_delta)。单次抓取没有 delta，
    # 用"非 idle 的累计占比"近似（首次抓取即可给出一致的量级，长期趋势准确）——
    # 这是与 Agent 的 cpu.percent（瞬时值）在语义上的差异，已在文档与 UI 说明。
    cpu_rows = parsed.get("node_cpu_seconds_total") or []
    if cpu_rows:
        total = sum(v for _, v in cpu_rows)
        idle = sum(v for labs, v in cpu_rows if labs.get("mode") == "idle")
        if total > 0:
            out["cpu.percent"] = round(max(0.0, min(100.0, (1 - idle / total) * 100)), 2)

    mem_avail = _first(parsed, "node_memory_MemAvailable_bytes")
    mem_total = _first(parsed, "node_memory_MemTotal_bytes")
    if mem_avail is not None and mem_total:
        out["mem.percent"] = round(max(0.0, min(100.0, (1 - mem_avail / mem_total) * 100)), 2)

    # 磁盘：取挂载点里"最紧"的那个（最需要告警的）
    sizes = parsed.get("node_filesystem_size_bytes") or []
    avails = {(labs.get("mountpoint"), labs.get("device")): v
              for labs, v in parsed.get("node_filesystem_avail_bytes") or []}
    worst = None
    for labs, size in sizes:
        if not size or labs.get("fstype") in ("tmpfs", "squashfs", "overlay", "ramfs"):
            continue
        avail = avails.get((labs.get("mountpoint"), labs.get("device")))
        if avail is None:
            continue
        used_pct = (1 - avail / size) * 100
        if worst is None or used_pct > worst:
            worst = used_pct
    if worst is not None:
        out["disk.percent"] = round(max(0.0, min(100.0, worst)), 2)

    for src, (dst, scale) in GAUGE_MAP.items():
        v = _first(parsed, src)
        if v is not None and dst not in out:
            out[dst] = round(v * scale, 4)

    # D163：node_exporter 的映射项也进**扁平视图**（取第一条序列）——
    # 「测试」按钮与整机级单值指标要能看到它们。带维度的完整视图见 map_to_platform_series。
    for src, dst in NODE_EXPORTER_MAP.items():
        v = _first(parsed, src)
        if v is not None and dst not in out:
            out[dst] = round(v, 4)

    # 其余按 common 前缀直接透传（数量可控，便于用户建自定义规则）
    for name, rows in parsed.items():
        if name in GAUGE_MAP or name in NODE_EXPORTER_MAP \
                or name.startswith(("node_", "go_", "process_", "promhttp_")):
            continue
        v = rows[0][1] if rows else None
        if v is not None:
            out.setdefault(name, round(v, 4))
    return out


# ──────────────────── D154（FR-422）：Categraf 指标映射表 ────────────────────
# 用户在阿里云语音那轮之后明确「**复用** Categraf」：在目标机上装 Categraf（它暴露
# Prometheus 格式的 /metrics），平台直采它的 /metrics（mode=scrape），把中间件/数据库
# 指标收进平台——**不自己写采集器**。
#
# 下面的指标名**全部来自夜莺官方集成定义**（integrations/*/metrics/categraf-base.json
# 与 integrations/*/dashboards/*.json 里的真实 PromQL），不是猜的：
#   mysql_global_status_threads_connected / mysql_global_variables_max_connections …
#   redis_connected_clients / redis_config_maxclients / redis_keyspace_hits …
#   postgresql_numbackends / postgresql_blks_hit …
#   nginx_active / nginx_requests …
# 维度标签同理：PG 是 {server, db}、Nginx 是 {server}（实测自夜莺集成定义）。
CATEGRAF_MAP: dict[str, str] = {
    # —— MySQL ——
    "mysql_global_status_threads_connected": "mysql.connections.current",
    "mysql_global_variables_max_connections": "mysql.connections.max",
    "mysql_global_status_threads_running": "mysql.threads.running",
    "mysql_global_status_queries": "mysql.queries.total",
    "mysql_global_status_slow_queries": "mysql.slow_queries.total",
    # —— Redis ——
    "redis_connected_clients": "redis.connections.current",
    "redis_config_maxclients": "redis.connections.max",
    "redis_used_memory": "redis.memory.used",
    "redis_maxmemory": "redis.memory.max",
    "redis_keyspace_hits": "redis.keyspace.hits",
    "redis_keyspace_misses": "redis.keyspace.misses",
    "redis_evicted_keys": "redis.evicted_keys.total",
    # —— PostgreSQL ——
    "postgresql_numbackends": "pg.connections.current",
    "postgresql_blks_hit": "pg.blks.hit",
    "postgresql_blks_read": "pg.blks.read",
    "postgresql_deadlocks": "pg.deadlocks.total",
    "postgresql_conflicts": "pg.conflicts.total",
    # —— Nginx ——
    "nginx_active": "nginx.connections.active",
    "nginx_reading": "nginx.connections.reading",
    "nginx_writing": "nginx.connections.writing",
    "nginx_waiting": "nginx.connections.waiting",
    "nginx_requests": "nginx.requests.total",
    "nginx_accepts": "nginx.accepts.total",
    "nginx_handled": "nginx.handled.total",
    # —— 主机层（Categraf 的 telegraf 风格名）——
    "mem_used_percent": "mem.percent",
    "disk_used_percent": "disk.percent",
    "system_load1": "load.1",
    "net_bytes_recv": "net.bytes_recv",
    "net_bytes_sent": "net.bytes_sent",
}

# 噪声标签（结构性、非业务维度）——保留它们会把一条序列炸成几十条。
# 业务维度（server / db / device / interface / address …）**一律保留**。
NOISE_LABELS = frozenset({"cpu", "mode", "fstype", "job", "url", "scheme", "quantile", "le"})

# D163：标签别名归一 —— 同一件事在不同 exporter 里叫法不同：
#   Categraf 的 net 插件用 `device` 表示**网卡**，node_exporter 用 `interface`。
#   不归一的话，用户写 `net.bytes_recv{interface=eth0}` 在 Categraf 源上永远不命中，
#   而在 node_exporter 源上命中 —— 同一张表里两套维度名，等于逼用户逐源记忆。
#
# ⚠️ 不能全局把 device 改成 interface：磁盘指标（disk.io.read.bytes）的 device
#   是**磁盘设备名**（vda/sr0），与网卡无关。归一**只对网络域指标生效**。
def _normalize_labels(labels: dict, dst_metric: str = "") -> dict:
    """标签名归一（别名 → 规范名）。只改键名，不改值。

    `dst_metric` 是映射后的平台指标名：网络域（net.*）才做 device→interface 归一。
    """
    if not labels:
        return {}
    if dst_metric.startswith("net.") and "device" in labels and "interface" not in labels:
        out = {}
        for k, v in labels.items():
            out["interface" if k == "device" else k] = v
        return out
    return labels


def _keep_labels(labels: dict, tag_keep: list[str] | None = None,
                 dst_metric: str = "") -> dict:
    """从原始 labels 里挑出**业务维度**。

    - `tag_keep` 非空 → 白名单模式（只留列出的标签）；
    - 否则 → 黑名单模式（丢掉 NOISE_LABELS，其余全留）。
    """
    if not labels:
        return {}
    labels = _normalize_labels(labels, dst_metric)
    if tag_keep:
        return {k: str(v) for k, v in labels.items() if k in set(tag_keep) and str(v) != ""}
    return {k: str(v) for k, v in labels.items() if k not in NOISE_LABELS and str(v) != ""}


def map_to_platform_series(parsed: dict, user_map: dict | None = None,
                           tag_keep: list[str] | None = None) -> list[tuple[str, dict, float]]:
    """把 exporter 指标映射成**带维度**的平台序列。

    返回 [(平台指标名, 维度字典, 值)]。

    与 `map_to_platform_metrics`（旧的、扁平 {metric: value}）的区别：
    旧函数把每条指标**只取第一条序列**（rows[0]）——多实例的中间件指标
    （PG 每个 db 一条、每台 PG 一条）会被压成一条，数据直接丢。
    本函数**保留 labels 作维度**，每条序列各存一份——这样
    「同一条规则在不同维度上产生独立事件」与「跨实例聚合」才成立（FR-424）。

    `user_map`（scrape_config.metric_map）可自定义映射，覆盖内置表。
    """
    out: list[tuple[str, dict, float]] = []
    # D163：node_exporter 主机层映射表也进 umap —— 但**必须走带维度路径**（②），
    # 不能走 GAUGE_MAP 的"取第一条"逻辑（那会把 eth0/lo、多块盘压成一条）。
    # 合并顺序：内置表 → node_exporter 表 → **用户自定义表**（用户永远最高优先级）。
    umap = {**CATEGRAF_MAP, **NODE_EXPORTER_MAP, **(user_map or {})}

    # ① 主机层聚合指标（CPU/内存/最紧磁盘）——沿用旧函数的算法，仍是整机级单值
    host_vals = map_to_platform_metrics(parsed)
    for name in ("cpu.percent", "mem.percent", "disk.percent"):
        if name in host_vals:
            out.append((name, {}, float(host_vals[name])))

    # ② Categraf / 中间件 / node_exporter 映射：**逐序列**保留维度
    #    （D163：node_* 的磁盘/网络/TCP 等是**多序列**——多块盘、多张网卡、
    #      每个 protocol 一条——必须逐条保留，走 ① 的"取第一条"会把它们压成一条）
    for raw_name, rows in parsed.items():
        dst = umap.get(raw_name)
        if not dst:
            continue
        for labs, v in rows:
            tags = _keep_labels(labs or {}, tag_keep, dst)
            out.append((dst, tags, float(v)))

    # ③ 未映射但用户显式白名单（--metric-keep）的指标：原样透传 + 保留维度
    return out


def _first(parsed: dict, name: str) -> float | None:
    rows = parsed.get(name)
    return rows[0][1] if rows else None


def extract_declared_metrics(text: str, patterns: list[str]) -> dict[str, float]:
    """按用户给的 `--metric-keep` 正则白名单，从原始文本里额外提取忠实数值。

    用途：用户想监控 node_exporter 的某个具体指标（如 `node_filefd_allocated`），
    但不在我们的映射表里。此时用正则逐个 value 行提取（不做单位换算，原样入库）。

    注意：这是**扁平视图**（同名多序列会被后者覆盖），仅供「测试」按钮预览与
    整机级单值指标使用。要保留维度下钻请用 `extract_declared_series`。
    """
    if not patterns:
        return {}
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if not any(re.fullmatch(p, name) or re.search(p, name) for p in patterns):
            continue
        try:
            out[name] = float(line.rsplit(" ", 1)[1])
        except (ValueError, IndexError):
            continue
    return out


def extract_declared_series(parsed: dict, patterns: list[str],
                            tag_keep: list[str] | None = None) -> list[tuple[str, dict, float]]:
    """白名单指标的**带维度**版本：[(指标名, 维度字典, 值)]，逐序列保留。

    D162-2（公网实测发现）：`extract_declared_metrics` 把同名多序列压成一条
    （`biz_order_queue_backlog{queue=pay}` 与 `{queue=trade}` 互相覆盖），
    且**维度全丢** —— 于是用户在自己的数据源上白名单了业务指标
    （`metric_keep: ["biz_"]`）之后，指标在库里查不到、也无法按维度下钻：
    「外部指标源」最核心的用途（按 queue/region 下钻）恰好失效。

    本函数复用 `_parse_exposition` 已解析好的 {name: [(labels, value)]} 结构，
    保留 labels 并逐条输出，白名单路径与内置映射表（CATEGRAF_MAP）行为对齐。
    """
    if not patterns:
        return []
    out: list[tuple[str, dict, float]] = []
    for raw_name, rows in parsed.items():
        if not any(re.fullmatch(p, raw_name) or re.search(p, raw_name) for p in patterns):
            continue
        for labs, v in rows:
            out.append((raw_name, _keep_labels(labs or {}, tag_keep, raw_name), float(v)))
    return out


async def fetch_metrics(ds: PromDatasource, path: str = "/metrics") -> str:
    """拉取 exporter 的 /metrics 文本。"""
    url = (ds.base_url or "").rstrip("/") + (path or "/metrics")
    headers = {}
    if ds.auth_type == "bearer" and ds.secret_encrypted:
        from app.services.llm_gateway import decrypt_secret

        headers["Authorization"] = f"Bearer {decrypt_secret(ds.secret_encrypted)}"
    auth = None
    if ds.auth_type == "basic" and ds.username:
        from app.services.llm_gateway import decrypt_secret

        auth = (ds.username, decrypt_secret(ds.secret_encrypted))
    async with httpx.AsyncClient(timeout=max(3, int(ds.timeout_s or 10))) as c:
        r = await c.get(url, headers=headers, auth=auth)
        r.raise_for_status()
        return r.text


async def scrape_once(ds: PromDatasource, path: str = "/metrics", keep: list[str] | None = None,
                      persist: bool = True, user_map: dict | None = None,
                      tag_keep: list[str] | None = None) -> dict:
    """抓一次并入库（ring + Host + 带维度序列）。返回 {ok, metrics, host_ident, samples, note}。

    D154（FR-422）：Categraf / 中间件指标**带维度**入库——同一指标的不同序列
    （PG 的每个 db、每台 server；磁盘的每个 device）各自成一条序列，
    不再被压成一条（多实例数据直接丢）。
    """
    try:
        text = await fetch_metrics(ds, path)
        parsed = _parse_exposition(text)
    except Exception as e:  # noqa: BLE001 —— 抓取失败只影响该数据源
        msg = f"{type(e).__name__}: {str(e)[:160]}"
        if persist:
            async with SessionLocal() as db:
                row = await db.get(PromDatasource, ds.id)
                if row:
                    row.last_check_status, row.last_check_at = "failed", datetime.utcnow()
                    await db.commit()
        return {"ok": False, "metrics": {}, "host_ident": "", "samples": 0, "note": f"抓取失败：{msg}"}

    # ① 带维度的序列映射（Categraf / 中间件 + 主机层）
    series = map_to_platform_series(parsed, user_map=user_map, tag_keep=tag_keep)
    values: dict[str, float] = {}
    for metric, tags, value in series:
        values.setdefault(metric, value)      # 扁平视图（给「测试」按钮看）取每指标第一个值
    # ② 用户显式白名单的指标：原样透传
    values.update(extract_declared_metrics(text, keep or []))
    # ②b D162-2：白名单指标也要**带维度**入库 —— 否则用户白名单了业务指标
    #（如 biz_order_queue_backlog{queue=pay|trade,region=…}）也查不到、无法下钻：
    # 「外部指标源」最核心的用途正是在这里，此前被扁平化路径吃掉了。
    # 去重：白名单可能命中映射表里已有的指标（同一指标两条路径各出一条），
    # 同名同维度只保留一条，避免 ring 里出现重复点。
    seen_keys = {(m, ring.tags_key(t)) for m, t, _ in series}
    for metric, tags, value in extract_declared_series(parsed, keep or [], tag_keep):
        key = (metric, ring.tags_key(tags))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        series.append((metric, tags, value))
    if not values:
        return {"ok": False, "metrics": {}, "host_ident": "", "samples": len(parsed),
                "note": "解析成功但没有可识别的指标（exporter 未暴露预期指标？）"}

    ident = SCRAPE_IDENT.format(did=ds.id)
    now = datetime.utcnow()
    for metric, value in values.items():
        if not metric:
            continue
        ring.append_point(ident, metric, now, value)
    # ③ 带维度的点单独入 ring 的带维度区（FR-424）
    for metric, tags, value in series:
        if tags:
            ring.append_point(ident, metric, now, value, tags=tags)

    if persist:
        async with SessionLocal() as db:
            host = (await db.execute(
                __import__("sqlalchemy").select(Host).where(Host.ident == ident))).scalar_one_or_none()
            if host is None:
                host = Host(ident=ident, name=f"[scrape] {ds.name}", ip="", os="exporter",
                            status="up", agent_version="prometheus-scrape", tags={"source": "scrape"})
                db.add(host)
            host.status = "up"
            host.last_beat = now
            row = await db.get(PromDatasource, ds.id)
            if row:
                row.last_check_status, row.last_check_at = "ok", now
            await db.commit()
    return {"ok": True, "metrics": values, "host_ident": ident, "samples": len(parsed), "note": ""}


async def scrape_all_enabled() -> int:
    """调度任务入口（全局兜底 tick）：抓取所有"到期"的 scrape 数据源。

    D161（靶机真机测试发现）：此前此函数**无视每源独立间隔**，每 30s 无差别拉全部
    scrape 数据源——导致 `scrape_interval_s=60/300` 被覆盖成 30s、`=0`（不自动拉）被
    覆盖成 30s 拉不停。修复：按每源的 scrape_interval_s 与 last_check_at 判定是否"到期"，
    未到期跳过；0 = 永不自动拉。独立任务（prom-scrape-{id}）不受影响。
    """
    from sqlalchemy import select

    from datetime import datetime as _dt

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(PromDatasource).where(PromDatasource.enabled.is_(True)))).scalars().all()
    ok = 0
    now = _dt.utcnow()
    # D162-1：已注册独立 APScheduler 任务的源由独立任务按其间隔精确拉取，
    # 全局 tick 让位——否则同源被两条路径各拉一次（拉取量翻倍、last_check_at 互相干扰）。
    try:
        from app.sched import _SCRAPE_JOB_DS_IDS
        dedicated = set(_SCRAPE_JOB_DS_IDS)
    except Exception:  # noqa: BLE001 —— 避免与 sched 循环 import
        dedicated = set()
    for ds in rows:
        if (getattr(ds, "mode", "query") or "query") != "scrape":
            continue
        # D161：尊重每源的拉取间隔。0 = 不自动拉；其余按 last_check_at + interval 判到期。
        interval_s = int(getattr(ds, "scrape_interval_s", 30) or 0)
        if interval_s <= 0:
            continue
        if ds.id in dedicated:
            continue      # 有独立任务，跳过（由 prom-scrape-{id} 精确按间隔拉）
        last = getattr(ds, "last_check_at", None)
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < interval_s:
                continue   # 未到期
        cfg = getattr(ds, "scrape_config", None) or {}
        res = await scrape_once(ds, cfg.get("path", "/metrics"), cfg.get("metric_keep") or [],
                                user_map=cfg.get("metric_map") or {},
                                tag_keep=cfg.get("tag_keep") or None)
        if res["ok"]:
            ok += 1
        else:
            log.warning("直采数据源 %s 失败：%s", ds.name, res["note"])
    return ok
