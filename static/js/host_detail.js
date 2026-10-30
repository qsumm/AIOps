// host_detail.js —— 主机详情抽屉（D41 抽独立模块 → D118 监控面板页签化，对标阿里云四页监控）
// 页签：概览 / 基础监控 / 操作系统 / 网络 / 进程 / 健康诊断。
// 懒加载：切到某签才发请求；切走即 dispose 全部图表实例（防 ECharts 泄漏）。
import { api } from "./api.js";
import { h, table, statusBadge, drawer, chart, baseAxis, themeColors, fmtTime, toast, fill, realDisks, fmtBytes, busy } from "./ui.js";

const charts = [];
let hostChartRange = 60;   // 分钟；默认最近 1h
let currentHostIdent = null;
let activeTab = "overview";

function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v)); }

function rTypeLabel(probes) {
  return probes.some((p) => p.type === "https-cert") ? "延迟ms / 证书剩余天" : "延迟(ms)";
}

/** X 轴标签：后端 points[0] 是无时区 UTC ISO（ring 存 utcnow naive）→ 解析成真实 UTC moment，
 *  再显式转 Asia/Shanghai（服务器本地时间）时分秒；跨天带 MM-DD。与 ui.fmtTime 同口径。 */
function axisLabel(isoStr, minutes) {
  const d = new Date(String(isoStr).replace(" ", "T") + (String(isoStr).includes("Z") ? "" : "Z"));
  if (isNaN(d.getTime())) return String(isoStr).slice(0, 5);
  const crossDay = minutes > 24 * 60;
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false,
    ...(crossDay && { month: "2-digit", day: "2-digit" }),
  }).formatToParts(d);
  const get = (type) => (parts.find((p) => p.type === type) || {}).value || "";
  const hm = `${get("hour")}:${get("minute")}`;
  return crossDay ? `${get("month")}-${get("day")} ${hm}` : hm;
}

/* P0：认证状态不是主机上下线状态。主机可能没有关机，但 Agent 凭证已失效——
   这时 last_beat 会变旧，必须单独显示，否则用户只能猜“是不是机器关了”。 */
function authBadge(state) {
  const s = String(state || "unknown");
  if (s === "ok") return h("span", { class: "badge PASS" }, "认证正常");
  if (s === "auth_failed") return h("span", { class: "badge FAIL" }, "认证失败");
  if (s === "needs_reinstall") return h("span", { class: "badge WARN" }, "需重新接入");
  return h("span", { class: "badge SKIP" }, "未知");
}

/* ─────────────── D118 监控图表基建 ─────────────── */

// 时间范围（分钟）：对齐阿里云 1h/3h/6h/12h/1d/7d/14d；30d 保留自大盘
const CH_RANGE = [["60", "1 小时"], ["180", "3 小时"], ["360", "6 小时"], ["720", "12 小时"],
  ["1440", "24 小时"], ["10080", "7 天"], ["20160", "14 天"], ["43200", "30 天"]];

const PALETTE = ["#3b82f6", "#10b981", "#06b6d4", "#8b5cf6", "#0ea5e9", "#6366f1", "#14b8a6", "#22d3ee"];

/** 按前缀发现 ring/agg 里已有的指标名（老 agent 无数据 → 空数组 → 面板显示占位）。 */
async function listMetrics(prefix) {
  try {
    const d = await api.get("/api/ops/metrics/list", { ident: currentHostIdent, prefix });
    return d.names || [];
  } catch { return []; }
}

/** 告警标记（FR-D2 故障追溯）：时间窗内的告警点，画在图上（每窗缓存一次）。 */
const _marksCache = {};
async function alertMarks(ident, minutes) {
  const key = `${ident}:${minutes}`;
  if (_marksCache[key]) return _marksCache[key];
  try {
    const d = await api.get(`/api/ops/hosts/${ident}/alerts/markers`, { minutes });
    _marksCache[key] = d.markers || [];
  } catch { _marksCache[key] = []; }
  return _marksCache[key];
}

/** 把告警时刻对齐到最近的序列点 → ECharts markPoint。
 *  文字不落图（曾把整条规则名叠在曲线上遮挡——用户实测），规则名收进悬停提示；
 *  颜色走主题（d50 冷色纪律）。 */
function marksToPoints(points, marks) {
  if (!points?.length || !marks?.length) return [];
  const c = themeColors();
  const ts = points.map((p) => new Date(String(p[0]).replace(" ", "T") + "Z").getTime());
  const out = [];
  for (const m of marks) {
    const t = new Date(String(m.t).replace(" ", "T") + "Z").getTime();
    if (isNaN(t)) continue;
    let best = 0, bd = Infinity;
    for (let i = 0; i < ts.length; i++) {
      const d = Math.abs(ts[i] - t);
      if (d < bd) { bd = d; best = i; }
    }
    if (bd < 10 * 60 * 1000)   // 偏差 10 分钟内才画（窗外标记无意义）
      out.push({ name: `${m.label}（${String(m.t).slice(5, 16)}）`, value: m.label,
                 coord: [best, points[best][1]], symbol: "pin", symbolSize: 9,
                 itemStyle: { color: m.severity === 1 ? c.fail : c.warn } });
  }
  return out;
}

async function querySeries(ident, metric, minutes) {
  try {
    const d = await api.get("/api/ops/metrics/query", { ident, metric, minutes });
    return (d.points && d.points.length) ? d.points : null;
  } catch { return null; }
}

/**
 * 监控图表面板工厂（D118，统一 tooltip/时间轴/图例/空态）。
 * spec = { series:[{name,metric}] } 或 { discover:{prefix, make(names)} }；
 * type: "line"（默认）| "stacked"（堆叠面积）；dual:{from:n} 第二个系列组走右轴。
 */
function monitorPanel(title, spec) {
  const box = h("div", { class: "chart-box-sm" });
  const card = h("div", { class: "card monitor-card" }, h("h3", {}, title), box);
  card._draw = async () => {
    const c = themeColors();
    const inst = chart(box);
    charts.push(inst);
    let sers = spec.series;
    if (spec.discover) {
      const names = await listMetrics(spec.discover.prefix);
      sers = spec.discover.make(names);
    }
    sers = sers || [];
    const pts = [];
    for (const s of sers) {
      const p = await querySeries(currentHostIdent, s.metric, hostChartRange);
      if (p) pts.push({ name: s.name, points: p });
    }
    if (!pts.length) {
      inst.setOption({ title: { text: spec.empty || "暂无指标数据（等待 Agent 升级采集）", left: "center", top: "middle",
        textStyle: { color: c.muted, fontSize: 12, fontWeight: "normal" } } });
      return;
    }
    const stack = spec.type === "stacked";
    const mpData = spec.marks ? marksToPoints(pts[0].points, await alertMarks(currentHostIdent, hostChartRange)) : [];
    inst.setOption({
      tooltip: { trigger: "axis" },
      legend: { data: pts.map((s) => s.name), textStyle: { color: c.muted, fontSize: 10 },
                top: 0, type: "scroll", pageIconColor: c.muted },
      grid: { left: 50, right: spec.dual ? 50 : 14, top: 34, bottom: 22 },
      xAxis: { type: "category", boundaryGap: false,
               data: pts[0].points.map((p) => axisLabel(p[0], hostChartRange)), ...baseAxis() },
      yAxis: [{ type: "value", ...baseAxis() }].concat(spec.dual ? [{ type: "value", ...baseAxis() }] : []),
      color: PALETTE,
      series: pts.map((s, i) => ({
        name: s.name, type: "line", smooth: true, showSymbol: false,
        data: s.points.map((p) => p[1]),
        ...(stack ? { stack: "total", areaStyle: { opacity: 0.3 } } : {}),
        ...(spec.dual && i >= spec.dual.from ? { yAxisIndex: 1 } : {}),
        ...(i === 0 && mpData.length ? { markPoint: {
          data: mpData,
          label: { show: false },            // 文字不落图（遮挡修复 D120）：规则名走悬停提示
          emphasis: { label: { show: false } },
        } } : {}),
        lineStyle: { width: 2 },
      })),
    });
  };
  return card;
}

/* 各页签图表面板定义（面板组 ↔ 需求 FR-B1/B2/B3 一一对应） */

const TAB_PANELS = {
  basic: () => [
    monitorPanel("CPU 使用率 (%)", { marks: true, series: [{ name: "CPU 使用率", metric: "cpu.percent" }] }),
    monitorPanel("内存使用率 (%)", { marks: true, series: [{ name: "内存使用率", metric: "mem.percent" }] }),
    monitorPanel("TCP 连接数 (Count)", { series: [
      { name: "总连接", metric: "net.tcp.total" }, { name: "ESTABLISHED", metric: "net.tcp.established" }] }),
    monitorPanel("磁盘读写 BPS (bytes/s)", { discover: {
      prefix: "disk.io.",
      make: (names) => [
        ...names.filter((n) => n.endsWith(".read_bps")).map((n) => ({ name: n.slice(8, -9) + " 读", metric: n })),
        ...names.filter((n) => n.endsWith(".write_bps")).map((n) => ({ name: n.slice(8, -10) + " 写", metric: n }))] } }),
    monitorPanel("磁盘读写 IOPS (Count/s)", { discover: {
      prefix: "disk.io.",
      make: (names) => [
        ...names.filter((n) => n.endsWith(".read_iops")).map((n) => ({ name: n.slice(8, -10) + " 读", metric: n })),
        ...names.filter((n) => n.endsWith(".write_iops")).map((n) => ({ name: n.slice(8, -11) + " 写", metric: n }))] } }),
    monitorPanel("inode 使用率 (%)", { discover: { prefix: "disk.inode.",
      make: (names) => names.map((n) => ({ name: n.slice(11, -8), metric: n })) },
      empty: "暂无 inode 指标（Linux agent 才采集）" }),
  ],
  os: () => [
    monitorPanel("CPU 分解 (%)", { type: "stacked", series: [
      { name: "用户态", metric: "cpu.user" }, { name: "系统态", metric: "cpu.system" },
      { name: "IO 等待", metric: "cpu.iowait" }] }),
    monitorPanel("内存可用 (%)", { series: [
      { name: "已用", metric: "mem.percent" }, { name: "可用", metric: "mem.available_percent" }] }),
    monitorPanel("缓冲/缓存 (MB)", { series: [
      { name: "Buffers", metric: "mem.buffers" }, { name: "Cached", metric: "mem.cached" }] }),
    monitorPanel("系统负载 (load)", { series: [
      { name: "load1", metric: "load.1" }, { name: "load5", metric: "load.5" }, { name: "load15", metric: "load.15" }] }),
    monitorPanel("按分区磁盘使用率 (%)", { discover: {
      prefix: "disk.",
      make: (names) => names.filter((n) => n.startsWith("disk.") && !n.startsWith("disk.io.")
          && !n.startsWith("disk.inode.")).map((n) => ({ name: "/" + n.slice(5, -8), metric: n })) } }),
    monitorPanel("分区读写 BPS (bytes/s)", { discover: {
      prefix: "disk.io.",
      make: (names) => [
        ...names.filter((n) => n.endsWith(".read_bps")).map((n) => ({ name: n.slice(8, -9) + " 读", metric: n })),
        ...names.filter((n) => n.endsWith(".write_bps")).map((n) => ({ name: n.slice(8, -10) + " 写", metric: n }))] } }),
  ],
  network: () => [
    monitorPanel("网卡带宽 (bytes/s)", { discover: { prefix: "net.",
      make: (names) => [
        ...names.filter((n) => n.endsWith(".in_bps") && !n.includes(".tcp")).map((n) => ({ name: n.slice(4, -7) + " 收", metric: n })),
        ...names.filter((n) => n.endsWith(".out_bps") && !n.includes(".tcp")).map((n) => ({ name: n.slice(4, -8) + " 发", metric: n }))] } }),
    monitorPanel("收发包数 (Count/s)", { discover: { prefix: "net.",
      make: (names) => [
        ...names.filter((n) => n.endsWith(".in_pps")).map((n) => ({ name: n.slice(4, -7) + " 收包", metric: n })),
        ...names.filter((n) => n.endsWith(".out_pps")).map((n) => ({ name: n.slice(4, -8) + " 发包", metric: n }))] } }),
    monitorPanel("错误与丢包 (Count/s)", { discover: { prefix: "net.",
      make: (names) => ["errin", "errout", "dropin", "dropout"].flatMap((k) =>
        names.filter((n) => n.endsWith(`.${k}`)).map((n) => ({ name: n.slice(4, -(k.length + 1)) + " " + k, metric: n }))) } }),
    monitorPanel("TCP 连接状态 (Count)", { type: "stacked", series: [
      { name: "ESTABLISHED", metric: "net.tcp.established" }, { name: "TIME_WAIT", metric: "net.tcp.time_wait" },
      { name: "LISTEN", metric: "net.tcp.listen" }, { name: "CLOSE_WAIT", metric: "net.tcp.close_wait" },
      { name: "SYN_SENT", metric: "net.tcp.syn_sent" }] }),
  ],
};

/* ─────────────── 概览页签（原有能力：meta/Agent 健康/磁盘分区/探活/端口进程） ─────────────── */

async function drawHostDisk(ident) {
  const box = document.getElementById("host-disk");
  if (!box) return;
  try {
    const d = await api.get(`/api/ops/hosts/${ident}`);
    const parts = realDisks(d.disks || []);
    if (!parts.length) { box.replaceChildren(h("div", { class: "muted" },
      "Agent 尚未上报分区明细（重装 Agent 后约 1 分钟内出现）。")); return; }
    const pctCls = (p) => p >= 85 ? "fail" : p >= 70 ? "warn" : "";
    const num = (v) => h("span", { class: "mono disk-num" }, fmtBytes(v));
    box.replaceChildren(table([
      { title: "Filesystem", render: (r) => h("span", { class: "mono" }, r.device || "-") },
      { title: "Size", width: "86px", render: (r) => num(r.size) },
      { title: "Used", width: "86px", render: (r) => num(r.used) },
      { title: "Avail", width: "86px", render: (r) => num(r.free) },
      { title: "Use%", width: "72px", render: (r) => h("span", { class: `mono disk-num ${pctCls(r.percent)}` }, `${Number(r.percent).toFixed(1)}%`) },
      { title: "Mounted on", render: (r) => h("span", { class: "mono" }, r.mount) },
    ], parts, {}));
  } catch { box.replaceChildren(h("div", { class: "muted" }, "磁盘指标暂不可用")); }
}

/* D146：机器配置（CPU 核数/型号、内存、磁盘合计、运行时长）。
   数据来自 Agent 随心跳上报的 hostinfo（平台落到 hosts 表）——
   旧 Agent（0.3.4 及以前）不上报 → 明确说"未上报 + 升级后可看"，不许显示假的 0。 */
function drawHostHardware(d) {
  const box = document.getElementById("host-hw");
  if (!box) return;
  const cores = Number(d.cpu_cores || 0);
  const phys = Number(d.cpu_cores_physical || 0);
  const mem = Number(d.mem_total || 0);
  const disks = realDisks(d.disks || []);
  const diskTotal = disks.reduce((s, p) => s + (Number(p.size) || 0), 0);
  const boot = d.boot_time ? new Date(String(d.boot_time).replace(" ", "T") + "Z") : null;
  const uptimeTxt = boot && !isNaN(boot.getTime())
    ? (() => {
        const s = Math.max(0, Math.floor((Date.now() - boot.getTime()) / 1000));
        const dd = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), mm = Math.floor((s % 3600) / 60);
        return dd > 0 ? `${dd} 天 ${hh} 小时` : (hh > 0 ? `${hh} 小时 ${mm} 分` : `${mm} 分钟`);
      })() : "";
  if (!cores && !mem) {
    box.replaceChildren(h("div", { class: "muted" },
      "当前 Agent 未上报硬件配置——升级到最新版 Agent（0.3.6+）后这里会显示 CPU 核数、内存与磁盘容量。"));
    return;
  }
  fill(box,
    kv("CPU", cores ? h("span", {}, `${cores} 核${phys && phys !== cores ? `（${phys} 物理核）` : ""}`) : "-"),
    kv("CPU 型号", d.cpu_model
      ? h("span", { class: "mono" }, d.cpu_model)
      : h("span", { class: "muted" }, "未暴露（部分云主机会屏蔽 CPU 型号）")),
    kv("内存", mem ? h("span", { class: "mono" }, fmtBytes(mem)) : "-"),
    kv("磁盘合计", diskTotal ? h("span", { class: "mono" }, fmtBytes(diskTotal)) : "-"),
    kv("已运行", uptimeTxt || "-"),
    kv("开机时间", boot && !isNaN(boot.getTime()) ? fmtTime(d.boot_time) : "-"));
}

async function drawOverview(body, host) {
  body.replaceChildren(
    h("div", { class: "detail-meta host-meta" },
      kv("状态", host.removed ? h("span", { class: "badge SKIP", title: "已移出：退役，Agent 停止上报，历史保留" }, "已移出")
        : host.offline ? h("span", { class: "badge WARN", title: "已下线：停止采集/告警，可恢复" }, "已下线")
        : statusBadge(host.status)),
      kv("IP", host.ip || "-"), kv("OS", host.os || "-"),
      kv("Agent 版本", host.agent_version || "-"), kv("最近心跳", fmtTime(host.last_beat)),
      kv("标识", h("span", { class: "mono", title: host.ident }, host.ident))),
    // D146：机器配置（几核几 G）——用户最常问的第一句
    h("div", { class: "card" }, h("h3", {}, "机器配置"),
      h("div", { id: "host-hw", class: "detail-meta" }, h("div", { class: "muted" }, "加载中…"))),
    h("div", { class: "card" }, h("h3", {}, "Agent 健康"),
      h("div", { class: "detail-meta" },
        kv("认证状态", authBadge(host.auth_state)),
        kv("版本状态", host.version_stale
          ? h("span", { class: "badge WARN", title: "在主机列表点「升级」生成重装命令" },
              `可升级到 ${host.latest_agent_version || "最新版"}`)
          : h("span", { class: "muted" }, "已是最新")),
        kv("最近成功上报", fmtTime(host.last_report_ok_at)),
        kv("缓存待传", `${host.cache_depth || 0} 条`),
        // D155：这里显示的是「**当前仍存在**」的错误——平台收到一次成功上报就会清掉它，
        // 所以它出现就说明此刻还没恢复（历史上出过的错去看审计日志，不在这里堆）。
        kv("当前错误", host.last_agent_error
          ? h("span", { class: "mono", title: host.last_agent_error, style: "color:var(--fail)" }, host.last_agent_error)
          : h("span", { class: "muted", title: "最近一次上报成功了，没有待处理的错误" }, "无")))),
    h("div", { class: "card" }, h("h3", {}, "磁盘分区（df -h）"),
      h("div", { id: "host-disk" }, h("div", { class: "empty" }, "加载中…"))),
    h("div", { class: "card" }, h("h3", {}, "服务探活"), h("div", { id: "host-probes" }, h("div", { class: "empty" }, "加载中…"))),
    h("div", { class: "card" }, h("h3", {}, "端口与进程"), h("div", { id: "host-services" }, h("div", { class: "empty" }, "加载中…"))));
  drawHostDisk(host.ident);
  api.get(`/api/ops/hosts/${host.ident}`).then((d) => {
    drawHostHardware(d);
    const probes = d.probes || [];
    document.getElementById("host-probes")?.replaceChildren(table([
      { title: "名称", render: (r) => r.name },
      { title: "类型", width: "92px", render: (r) => h("span", { class: "mono muted" }, r.type) },
      { title: "目标", render: (r) => h("span", { class: "mono" }, r.target) },
      { title: "状态", width: "80px", render: (r) => statusBadge(r.last_status) },
      { title: rTypeLabel(probes), width: "120px",
        render: (r) => h("span", { class: "mono" }, r.last_latency_ms == null ? "-" : `${r.last_latency_ms}`) },
    ], probes, { emptyText: "未配置探活——重新生成安装命令（接入新主机向导默认带 SSH 探活）或联系管理员添加" }));
    const sv = d.services || {};
    const ports = sv.ports || [], procs = sv.processes || [];
    const box = document.getElementById("host-services");
    if (!box) return;
    if (!ports.length && !procs.length) {
      box.replaceChildren(h("div", { class: "empty" },
        "暂无发现数据——Agent 会自动扫描监听端口与高占用进程（约每分钟刷新）；刚接入的主机请等 1~2 分钟。"));
      return;
    }
    fill(box,
      h("div", { class: "muted", style: "font-size:0.76rem;margin-bottom:6px" },
        `正在监听 ${ports.length} 个端口 · CPU 占用 Top ${procs.length}`),
      ports.length ? h("div", { class: "port-chips" },
        ports.map((p) => h("span", { class: "port-chip", title: `${p.addr}${p.proc ? " · " + p.proc : ""}` },
          h("span", { class: "mono" }, `:${p.port}`), p.proc ? h("span", { class: "muted" }, ` ${p.proc}`) : null))) : null,
      procs.length ? table([
        { title: "进程", render: (r) => h("span", { class: "mono" }, r.name) },
        { title: "CPU%", width: "80px", render: (r) => h("span", { class: "mono" }, String(r.cpu)) },
      ], procs, { emptyText: "" }) : null);
  }).catch(() => {});
}

/* ─────────────── 监控页签（基础/操作系统/网络） ─────────────── */

function drawMonitorTab(body, which) {
  const rangeBar = h("div", { class: "range-bar" },
    ...CH_RANGE.map(([min, label]) =>
      h("button", { class: `pill ${Number(min) === hostChartRange ? "active" : ""}`,
        onclick: () => { hostChartRange = Number(min); drawMonitorTab(body, which); } }, label)));
  const grid = h("div", { class: "monitor-grid" }, h("div", { class: "empty" }, "加载中…"));
  body.replaceChildren(
    h("div", { class: "card" }, h("div", { class: "card-head" },
      h("h3", {}, "时间范围"), h("span", { class: "muted", style: "font-size:0.74rem" },
        "1h~2h 走 15 秒明细；更长窗口自动切 5 分钟聚合")), rangeBar),
    grid);
  const panels = (TAB_PANELS[which] || (() => []))();
  grid.replaceChildren(...panels);
  for (const p of panels) p._draw();
}

/* ─────────────── 进程页签（FR-B4，对标阿里云进程监控表） ─────────────── */

async function drawProcessTab(body) {
  const wrap = h("div", { class: "card" }, h("h3", {}, "进程 TopN（CPU ∪ 内存）"),
    h("div", { class: "empty" }, "加载中…"));
  body.replaceChildren(wrap);
  try {
    const d = await api.get(`/api/ops/hosts/${currentHostIdent}`);
    const procs = (d.services || {}).processes || [];
    if (!procs.length) {
      wrap.lastChild.replaceChildren(h("div", { class: "empty" },
        "暂无进程快照——升级 Agent 后约 1 分钟出现（CPU TopN ∪ 内存 TopN 去重，每分钟刷新）。"));
      return;
    }
    const num = (v) => h("span", { class: "mono" }, v == null ? "-" : String(v));
    const col = (title, key, fmt) => ({ title: title + " ⇅", width: "110px",
      render: (r) => fmt ? fmt(r[key]) : num(r[key]) });
    const sortRows = (key) => [...procs].sort((a, b) => (Number(b[key]) || 0) - (Number(a[key]) || 0));
    const state = { key: "mem_percent" };
    const tbl = () => table([
      { title: "进程", render: (r) => h("span", { class: "mono host-name", title: r.name }, r.name) },
      { title: "PID", width: "90px", render: (r) => num(r.pid) },
      { title: "用户", width: "90px", render: (r) => h("span", { class: "muted mono" }, r.user || "-") },
      col("内存%", "mem_percent", (v) => h("span", { class: "mono" }, `${Number(v || 0).toFixed(1)}%`)),
      col("CPU%", "cpu", (v) => h("span", { class: "mono" }, `${Number(v || 0).toFixed(1)}%`)),
      col("打开文件数", "open_fds"),
      { title: "状态", width: "80px", render: (r) => h("span", { class: "mono muted" }, r.state || "-") },
      { title: "RSS", width: "90px", render: (r) => h("span", { class: "mono muted" }, fmtBytes(r.mem_rss)) },
    ], sortRows(state.key), {});
    const bar = h("div", { class: "range-bar" },
      ...[["mem_percent", "按内存"], ["cpu", "按 CPU"], ["open_fds", "按 fd 数"]].map(([k, label]) =>
        h("button", { class: `pill ${state.key === k ? "active" : ""}`,
          onclick: (e) => { state.key = k;
            [...e.currentTarget.parentElement.children].forEach((b) => b.classList.remove("active"));
            e.currentTarget.classList.add("active"); tblBox.replaceChildren(tbl()); } }, label)));
    const tblBox = h("div", {});
    tblBox.replaceChildren(tbl());
    wrap.replaceChildren(h("div", { class: "card-head" },
      h("h3", {}, "进程 TopN（CPU ∪ 内存去重）"),
      h("span", { class: "muted", style: "font-size:0.74rem" }, "每分钟随 Agent 上报刷新")), bar, tblBox);
  } catch (e) {
    wrap.lastChild.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`));
  }
}

/* ─────────────── 健康诊断页签（D119，FR-C1~C4；对标阿里云「实例问题排查」） ─────────────── */

const DIAG_SCOPE = ["性能", "存储", "网络", "服务"];
const ST_BADGE = { pass: ["PASS", "正常"], warn: ["WARN", "告警"], fail: ["FAIL", "异常"], skip: ["SKIP", "未采集"], error: ["FAIL", "执行异常"] };
const OVERALL = { healthy: ["PASS", "健康"], attention: ["WARN", "需关注"], unhealthy: ["FAIL", "异常"] };

function diagCard(title, desc, checks) {
  return h("div", { class: "card diag-card" },
    h("h3", {}, title), h("div", { class: "muted", style: "font-size:0.76rem" }, desc),
    h("div", { class: "diag-checks" }, ...checks.map((c) => h("span", { class: "port-chip muted" }, c))));
}

function renderReport(data) {
  const [oCls, oTxt] = OVERALL[data.summary?.overall] || ["SKIP", "未知"];
  const counts = data.summary || {};
  const grouped = {};
  for (const it of (data.items || [])) (grouped[it.scope] ||= []).push(it);
  return h("div", { class: "diag-report" },
    h("div", { class: "detail-meta" },
      kv("整体结论", h("span", { class: `badge ${oCls}` }, oTxt)),
      kv("报告 ID", h("span", { class: "mono" }, `#${data.id}`)),
      kv("数据窗", `${(data.window_start || "").slice(0, 19)} ~ ${(data.window_end || "").slice(0, 19)}`),
      kv("统计", `正常 ${counts.pass || 0} · 告警 ${counts.warn || 0} · 异常 ${counts.fail || 0} · 未采集 ${counts.skip || 0}`)),
    ...DIAG_SCOPE.filter((s) => grouped[s]?.length).map((s) =>
      h("div", { class: "card" }, h("h3", {}, s),
        ...grouped[s].map((it) => {
          const [cls, txt] = ST_BADGE[it.status] || ["SKIP", it.status];
          return h("div", { class: "diag-item" },
            h("div", { class: "diag-item-head" },
              h("span", { class: `badge ${cls}` }, txt),
              h("span", { class: "diag-title" }, it.title),
              h("span", { class: "mono muted", style: "font-size:0.72rem" }, `${it.elapsed_ms}ms`)),
            h("div", { class: "diag-conclusion" }, it.conclusion),
            (it.evidence || []).length ? h("details", { class: "diag-evidence" },
              h("summary", {}, `证据（${it.evidence.length}）`),
              ...it.evidence.map((e) => h("div", { class: "diag-ev-row mono" },
                h("span", { class: "muted" }, e.token), " → ", e.detail))) : null);
        }))));
}

function drawDiagTab(body) {
  const resultBox = h("div", {});
  const historyBox = h("div", {});
  body.replaceChildren(
    h("div", { class: "diag-cards" },
      diagCard("实例性能问题", "CPU、内存或系统负载持续偏高（超 80%），定位高占用进程与 IO 等待", ["CPU 使用率", "内存与 OOM 追溯", "系统负载匹配"]),
      diagCard("无法连接或启动异常", "探活失败、服务 down、TCP 连接异常——连通性与可用性第一现场", ["探活状态", "服务可用性", "TCP 连接健康"]),
      diagCard("存储与容量风险", "磁盘/ inode 水位、IO 饱和、网卡错误丢包——容量与传输层体检", ["磁盘水位", "磁盘 IO 饱和", "inode 水位", "网卡错误与丢包"])),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h3", {}, "一键诊断"),
        h("span", { class: "muted", style: "font-size:0.74rem" },
          "规则诊断树 · 只读平台数据 · 不依赖 AI 额度，约 2 秒出报告")),
      h("div", { class: "detail-actions" },
        h("button", { class: "btn", onclick: async (e) => {
          try {
            const r = await busy(e.currentTarget, "诊断中…",
              () => api.post(`/api/ops/hosts/${currentHostIdent}/diagnose/health`, { minutes: 60 }));
            toast(`诊断完成：报告 #${r.data?.report_id ?? r.report_id ?? ""}`, "ok");
            resultBox.replaceChildren(renderReport(r.data || r));
            loadHistory();
          } catch (err) { toast(`诊断失败：${err.message}`, "error"); }
        } }, "一键诊断")),
      h("div", { class: "muted", style: "font-size:0.74rem" },
        "覆盖 10 项检查：CPU / 内存(OOM 追溯) / 负载 / 磁盘水位 / IO 饱和 / inode / TCP / 网卡错误丢包 / 探活 / 服务可用性")),
    resultBox,
    h("div", { class: "card" }, h("h3", {}, "历史诊断"), historyBox));

  async function loadHistory() {
    try {
      const d = await api.get(`/api/ops/hosts/${currentHostIdent}/diagnose/health/reports`);
      const items = d.items || [];
      if (!items.length) { historyBox.replaceChildren(h("div", { class: "muted" }, "暂无历史报告——点上方「一键诊断」发起")); return; }
      historyBox.replaceChildren(table([
        { title: "报告", width: "80px", render: (r) => h("span", { class: "mono" }, `#${r.id}`) },
        { title: "时间", render: (r) => h("span", { class: "muted" }, (r.started_at || "").replace("T", " ").slice(0, 19)) },
        { title: "结论", width: "90px", render: (r) => {
            const [cls, txt] = OVERALL[r.summary?.overall] || ["SKIP", r.status];
            return h("span", { class: `badge ${cls}` }, txt); } },
        { title: "明细", width: "200px", render: (r) => {
            const c = r.summary || {};
            return h("span", { class: "muted", style: "font-size:0.74rem" },
              `正常 ${c.pass || 0} · 告警 ${c.warn || 0} · 异常 ${c.fail || 0}`); } },
        { title: "", width: "90px", render: (r) => h("button", { class: "btn ghost sm",
            onclick: async () => {
              try {
                const det = await api.get(`/api/ops/hosts/${currentHostIdent}/diagnose/health/reports/${r.id}`);
                resultBox.replaceChildren(renderReport(det));
                resultBox.scrollIntoView({ behavior: "smooth", block: "start" });
              } catch (err) { toast(`加载报告失败：${err.message}`, "error"); }
            } }, "查看报告") },
      ], items, {}));
    } catch (e) { historyBox.replaceChildren(h("div", { class: "muted" }, `加载历史失败：${e.message}`)); }
  }
  loadHistory();
}

/* ─────────────── 页签框架 ─────────────── */

const TABS = [["overview", "概览"], ["basic", "基础监控"], ["os", "操作系统"],
  ["network", "网络"], ["process", "进程"], ["diag", "健康诊断"]];

function drawActiveTab(body, host) {
  for (const c of charts.splice(0)) c.dispose();   // 切签先释放旧实例
  if (activeTab === "overview") drawOverview(body, host);
  else if (activeTab === "basic" || activeTab === "os" || activeTab === "network") drawMonitorTab(body, activeTab);
  else if (activeTab === "process") drawProcessTab(body);
  else if (activeTab === "diag") drawDiagTab(body);
}

/** 打开主机详情抽屉（D118：页签化监控面板 + 概览）。 */
export function showHostDetail(host) {
  if (!host) return;
  currentHostIdent = host.ident;
  activeTab = "overview";
  const bar = h("div", { class: "host-tabs" },
    ...TABS.map(([k, label]) =>
      h("button", { class: `btn ghost sm${k === activeTab ? " active" : ""}`,
        onclick: (e) => {
          activeTab = k;
          [...e.currentTarget.parentElement.children].forEach((b) => b.classList.remove("active"));
          e.currentTarget.classList.add("active");
          drawActiveTab(body, host);
        } }, label)));
  const body = h("div", { class: "detail-body-scroll" });
  drawer(`主机详情 · ${host.name || (host.ident || "").slice(0, 8)}`, bar, body);
  drawActiveTab(body, host);
}

/** 传 ident 打开该主机详情（先拉主机对象）。供大盘等跨页调用。
 *  注意：`GET /api/ops/hosts/{ident}` 返回的是**扁平**主机对象（顶层即 name/status/probes），
 *  没有嵌套的 host 字段——曾经按 `d.host` 取值走了兜底，导致标题显示 ident 短哈希（D41 实测）。 */
export async function openHostDetail(ident) {
  try {
    const d = await api.get(`/api/ops/hosts/${ident}`);
    showHostDetail(d && (d.name || d.ident) ? d : { ident, name: ident.slice(0, 8) });
  } catch (e) { toast(`打开主机详情失败：${e.message}`, "error"); }
}

export function destroy() { charts.forEach((c) => c.dispose()); charts.length = 0; }