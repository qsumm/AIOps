// pages/dashboard.js —— 总览大盘（FR-F ops-dashboard）
// 必备要素：健康度卡片（主机/告警/事件计数）、核心指标图（随主题）、活跃告警列表、AI 摘要位、今日巡检状态
// D26 增强：30s 自动刷新 · 时间范围 1h/6h/24h · 告警抽屉显示 AI 摘要 · 宕机主机点名 ·
//           Top5 CPU · 24h 事件直方图 · 未闭环问题 · 平均恢复时长 · 巡检发现摘要
import { api } from "../api.js";
import { marked } from "/vendor/marked.esm.js";   // D126：AI 摘要带 **加粗**，同告警中心一致走 markdown
import { openHostDetail } from "../host_detail.js";   // D41：点击直达主机详情
import { h, table, sevBadge, chart, baseAxis, themeColors, relTime, fmtTime, drawer, hostLabel, hostIp, ensureHostNames, statCard, tooltipStyle, thLabel, helpTip, toast, realDisks } from "../ui.js";

/** 主机显示名（含 IP 注释）：`名称 (IP)`，IP 拿不到时只显示名称。 */
function hostWithIp(ident) {
  const ip = hostIp(ident);
  return ip ? `${hostLabel(ident)} (${ip})` : hostLabel(ident);
}

/** 后端 core 指标点的 X 轴标签：p[0] 是 UTC ISO 串 → 服务器本地（+8 北京）时分秒。
 *  短窗口（≤24h）显示 `HH:MM`；跨越多天显示 `MM-DD HH:MM`，避免一眼分不清哪天。 */
function axisLabel(isoStr, windowDays) {
  const d = new Date(String(isoStr).replace(" ", "T") + (String(isoStr).includes("Z") ? "" : "Z"));
  if (isNaN(d.getTime())) return String(isoStr).slice(0, 5);
  const crossDay = windowDays > 1;
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false,
    ...(crossDay && { month: "2-digit", day: "2-digit" }),
  }).formatToParts(d);
  const get = (type) => (parts.find((p) => p.type === type) || {}).value || "";
  const hm = `${get("hour")}:${get("minute")}`;
  return crossDay ? `${get("month")}-${get("day")} ${hm}` : hm;
}

let charts = [];
let timer = null;
let curRange = "1h";
// 预设档位：小时/天级都走 metric_agg 时 5m 桶，档位越多用户选择越灵活
const RANGES = [
  ["1h", "1 小时"], ["6h", "6 小时"], ["24h", "24 小时"],
  ["7d", "7 天"], ["30d", "30 天"],
];
const REFRESH_S = 30;
let clockTimer = null;   // "当前北京时间"秒级时钟（D107：随 destroy 清理）
// 北京时间格式化：显式指定 Asia/Shanghai——查看者机器在任何时区都显示同一钟面
const fmtBeijing = (d) => new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "long", day: "numeric",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(d);

function nav(hash) { location.hash = hash; }

function card(label, value, sub, cls = "", onclick = null) {
  // D42：复用 ui.js 的 statCard（含"数值/单位"分层），仅额外挂 onclick
  const el = statCard(label, value, sub, cls);
  if (onclick) { el.style.cursor = "pointer"; el.onclick = onclick; }
  return el;
}

export async function render(root) {
  destroy(); // 重入（自动刷新/切范围）先释放图表与定时器
  document.getElementById("content")?.classList.add("dash-page");
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));
  await ensureHostNames();   // D40：主机 ident → 主机名 映射（否则显示一串哈希看不懂）
  let ov;
  try {
    ov = await api.get(`/api/ops/dashboard/overview?range=${curRange}`);
  } catch (e) { root.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }

  const hosts = ov.hosts || { total: 0, up: 0, down_hosts: [] };
  const alertCounts = ov.alerts || { total: 0, by_severity: {} };
  const healthRate = hosts.total ? Math.round((hosts.up / hosts.total) * 100) : 100;
  const healthCls = healthRate >= 99 ? "pass" : healthRate >= 90 ? "warn" : "fail";
  const downList = hosts.down_hosts || [];
  const downSub = downList.length
    ? `宕机：${downList.slice(0, 2).map((x) => hostWithIp(x)).join("、")}${downList.length > 2 ? " 等" : ""} · 点击查看`
    : `${hosts.up}/${hosts.total} 在线`;
  const mttr = ov.mttr_minutes;
  const mttrText = mttr == null ? "—" : mttr >= 60 ? `${(mttr / 60).toFixed(1)} 小时` : `${mttr} 分钟`;
  const insp = ov.inspection;
  const topFinding = insp && insp.top_findings && insp.top_findings.length
    ? (insp.top_findings[0].label || insp.top_findings[0].item || "") : "";

  const grid = h("div", { class: "stat-grid" },
    card("主机健康率", `${healthRate}%`, downSub, healthCls, () => nav("#/hosts")),
    card("活跃告警", alertCounts.total ?? 0,
      `S1 ${alertCounts.by_severity?.["1"] || 0} · S2 ${alertCounts.by_severity?.["2"] || 0} · S3 ${alertCounts.by_severity?.["3"] || 0}`,
      alertCounts.total ? "fail" : "pass", () => nav("#/alerts")),
    card("24h 事件", ov.events_24h ?? 0, "含已恢复 · 点击看分布", "accent", () => {
      const el = document.getElementById("dash-events");
      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    }),
    card("巡检",
      insp ? (insp.status === "ok" ? "正常" : insp.status === "running" ? "执行中" : "待执行") : "未配置",
      insp
        ? `${insp.last_run_at ? "最近 " + relTime(insp.last_run_at) : "尚未执行"}${insp.anomalies ? ` · 异常 ${insp.anomalies}` : ""}${topFinding ? ` · ${topFinding}` : ""}`
        : "去「巡检报告」创建任务",
      insp && insp.anomalies ? "warn" : "accent", () => nav("#/inspections")),
    card("未闭环问题", ov.incidents_open ?? 0, "待处理 / 已确认 · 点击去处置",
      (ov.incidents_open ?? 0) ? "warn" : "pass", () => nav("#/incidents")),
    card("平均恢复时长", mttrText, "近 7 天已恢复告警",
      mttr == null ? "" : mttr <= 30 ? "pass" : "warn"));

  const rangeBar = h("div", { class: "range-bar" },
    ...RANGES.map(([r, label]) =>
      h("button", { class: `pill ${r === curRange ? "active" : ""}`,
        onclick: () => { curRange = r; render(root); } }, label)),
    h("input", { class: "range-custom", type: "text", placeholder: "自定义如 12h/90d",
      onkeydown: (e) => {
        if (e.key !== "Enter") return;
        const v = (e.target.value || "").trim();
        if (/^\d+[hd]$/i.test(v)) { curRange = v.toLowerCase(); render(root); }
        else { toast("请输入小时/天，如 `12h` 或 `90d`", "error"); }
      } }));
  const chartCard = h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("h3", {}, "核心指标 · 集群平均 CPU / 内存"), rangeBar),
    h("div", { class: "chart-box", id: "dash-chart" }));
  const alertCard = h("div", { class: "card" },
    h("h3", {}, "活跃告警"),
    h("div", { id: "dash-alerts" }, h("div", { class: "empty" }, "加载中…")));
  const eventsCard = h("div", { class: "card" },
    h("h3", {}, "24h 事件分布 · 逐小时"),
    h("div", { class: "chart-box sm", id: "dash-events" }));
  const topCpuCard = h("div", { class: "card" },
    h("h3", {}, "接近阈值的主机"),
    h("div", { class: "muted", style: "font-size:0.74rem;margin:-4px 0 8px" },
      "按你配置的告警规则实时评估：已触发的排最前，其次是逼近阈值(≥80%)的。点击查看该主机详情。"),
    h("div", { id: "dash-topcpu" }));
  const summaries = (ov.active_alerts || []).filter((a) => (a.ai_summary || "").trim()).slice(0, 3);
  const aiCard = h("div", { class: "card" },
    h("h3", {}, "AI 摘要 · 告警根因速览"),
    h("div", { class: "ai-brief" },
      summaries.length
        ? summaries.map((a) => h("div", { class: "ai-brief-item clickable", onclick: () => showAlertDrawer(a) },
            h("span", { class: "mono muted" }, `${hostWithIp(a.host_ident)} · ${a.metric}`),
            h("p", { html: marked.parse(a.ai_summary) })))
        : h("div", { class: "empty" }, "暂无 AI 摘要 —— 在「告警中心」对告警执行诊断后自动生成")));

  // P5（FR-408）：服务健康 —— 与主机健康率并列，直接回答"哪个服务不对劲"
  const svcCard = h("div", { class: "card" }, h("h3", {}, "服务健康"),
    h("div", { class: "svc-dash", id: "svc-health" }, h("div", { class: "empty" }, "加载中…")));

  // 磁盘使用率（df -h 一眼扫）：每台 up 主机的每个真实分区，红色警告超 85%
  const diskRows = [];
  for (const d of ov.disks || []) {
    for (const p of realDisks(d.partitions)) {
      diskRows.push({ ident: d.ident, host: d.host, mount: p.mount || "/", percent: Number(p.percent) || 0 });
    }
  }
  const diskCard = h("div", { class: "card" }, h("h3", {}, "磁盘使用率"),
    diskRows.length
      ? h("div", { class: "disk-dash" },
          ...diskRows.slice(0, 12).map((d) => {
            const danger = d.percent >= 85, warn = d.percent >= 70;
            const mount = d.mount === "/" ? "根分区 /" : d.mount;
            return h("div", { class: "disk-row", title: `${d.host} · ${d.mount}`, onclick: () => openHostDetail(d.ident) },
              h("span", { class: "disk-host mono", style: "min-width:108px" }, hostWithIp(d.ident)),
              h("span", { class: "disk-mnt mono muted" }, mount),
              h("div", { class: "disk-bar" },
                h("i", { style: `width:${Math.min(d.percent, 100)}%;background:${danger ? "var(--fail)" : warn ? "var(--warn)" : ""}` })),
              h("span", { class: `disk-pct mono ${danger ? "fail" : ""}` }, `${d.percent.toFixed(1)}%`));
          }),
          diskRows.length > 12
            ? h("div", { class: "muted", style: "font-size:0.72rem;margin-top:6px" }, `另有 ${diskRows.length - 12} 个分区 · 点主机进详情看全部`)
            : null)
      : h("div", { class: "muted" }, "暂未采集到磁盘指标（等待 Agent 上报）"));

  // 资源 Top5（D119/M4.1，FR-D1）：内存 / 磁盘 / 网络吞吐多主机排行（在线主机）
  const fmtBps = (v) => v >= 1048576 ? `${(v / 1048576).toFixed(1)} MB/s`
    : v >= 1024 ? `${(v / 1024).toFixed(1)} KB/s` : `${Math.round(v)} B/s`;
  const topRow = (k, fmt) => (ov.top5?.[k] || []).slice(0, 5).map((r, i) =>
    h("div", { class: "disk-row", onclick: () => openHostDetail(r.ident) },
      h("span", { class: "mono muted", style: "width:16px" }, String(i + 1)),
      h("span", { class: "disk-host mono", style: "min-width:100px" }, r.name),
      h("div", { class: "disk-bar" },
        h("i", { style: `width:${Math.min(100, (r.value / ((ov.top5[k][0]?.value) || 1)) * 100)}%` })),
      h("span", { class: "disk-pct mono" }, fmt ? fmt(r.value) : `${r.value}%`)));
  const top5Card = h("div", { class: "card" }, h("h3", {}, "资源 Top5（在线主机）"),
    (ov.top5 && (ov.top5.mem?.length || ov.top5.disk?.length || ov.top5.net?.length))
      ? h("div", { class: "top5-grid" },
          ...[["内存使用率", "mem", null], ["磁盘使用率", "disk", null], ["网络吞吐", "net", fmtBps]].map(([t, k, f]) =>
            h("div", { class: "top5-col" },
              h("div", { class: "muted", style: "font-size:0.74rem;margin-bottom:6px" }, t),
              (ov.top5[k] || []).length ? topRow(k, f) : h("div", { class: "muted", style: "font-size:0.72rem" }, "暂无数据"))))
      : h("div", { class: "muted" }, "暂无多主机资源数据（等待 Agent 上报）"));

  // D113「待我处理」：主动暴露需要人确认/订正/处理的事项（不再等人自己翻页面）
  const todoCard = h("div", { class: "card", id: "todo-card" },
    h("div", { class: "empty" }, "加载中…"));

  root.replaceChildren(
    h("div", { class: "page-head" },
      h("span", { class: "count-pill" },
        `最近一次更新：${fmtBeijing(new Date())} · 每 ${REFRESH_S}s 自动刷新`,
        h("span", { style: "margin:0 6px", class: "muted" }, "|"),
        h("span", {}, "当前北京时间："),
        h("span", { class: "mono", id: "bj-clock" }, fmtBeijing(new Date())))),
    todoCard,
    grid,
    svcCard,
    h("div", { class: "dash-duo" }, diskCard, top5Card),
    h("div", { class: "two-col" }, chartCard, alertCard),
    h("div", { class: "two-col" }, eventsCard, topCpuCard),
    aiCard);

  // D45 架构改进：**块级隔离**——任一区块渲染失败只影响它自己并在块内提示，
  // 不再像此前那样"一个图表抛错 → 其后所有区块都不执行"（那次让大盘半屏空白）。
  /** 服务健康块（独立加载：失败只影响本块，不影响大盘其余部分）。 */
  async function drawSvcHealth() {
    const el = document.getElementById("svc-health");
    if (!el) return;
    let d = null;
    try { d = await api.get("/api/ops/services/overview"); } catch { /* ignore */ }
    if (!d) { el.replaceChildren(h("div", { class: "empty" }, "服务数据暂不可用")); return; }
    if (!d.total) {
      el.replaceChildren(h("div", { class: "muted" },
        "尚未发现服务对象——Agent 会自动识别本机的 MySQL / Redis / Nginx / 消息队列"));
      return;
    }
    const b = d.by_status || {};
    el.replaceChildren(
      h("div", { class: "svc-dash-row" },
        h("span", { class: "svc-dash-num" }, String(d.total)), h("span", { class: "muted" }, "个服务")),
      h("div", { class: "svc-dash-chips" },
        chip("正常", b.up || 0, "pass"), chip("异常", b.degraded || 0, "warn"),
        chip("不可用", b.down || 0, "fail"), chip("未知", b.unknown || 0, "skip")),
      (d.unhealthy || []).length
        ? h("div", { class: "svc-dash-bad" },
            ...d.unhealthy.slice(0, 4).map((x) => h("div", { class: "clickable", onclick: () => { location.hash = "#/services"; } },
              h("span", { class: "mono muted" }, x.host), " / ", x.name,
              h("span", { class: "muted" }, ` · ${x.note ? String(x.note).slice(0, 34) : x.status}`))))
        : h("div", { class: "muted" }, "全部正常"));
  }
  function chip(label, n, cls) {
    return h("span", { class: `svc-chip ${cls}` }, h("b", {}, String(n)), label);
  }

  function block(name, fn) {
    try { fn(); }
    catch (e) {
      console.error(`[dashboard] 区块「${name}」渲染失败`, e);
      const map = { "核心指标图": "dash-chart", "事件分布图": "dash-events", "活跃告警": "dash-alerts", "接近阈值主机": "dash-topcpu" };
      const el = document.getElementById(map[name]);
      if (el) el.replaceChildren(h("div", { class: "empty" }, "该区块暂时无法显示，其余数据不受影响"));
    }
  }

  block("核心指标图", () => renderSeriesChart(document.getElementById("dash-chart"), ov.series || []));
  // D45 防御：events_by_hour 必须是数组；后端一旦回传异常结构，也不能让整页渲染中断
  const hours = Array.isArray(ov.events_by_hour) ? ov.events_by_hour : [];
  block("事件分布图", () => renderEventsChart(document.getElementById("dash-events"), hours));

  const alerts = ov.active_alerts || [];
  block("活跃告警", () => document.getElementById("dash-alerts").replaceChildren(
    table([
      { title: thLabel("级别", "**S1 紧急** = 最严重，需立即处理；**S2 警告** = 影响明显，尽快处理；**S3 通知** = 记录与观察"), width: "92px", render: (r) => sevBadge(r.severity) },
      { title: "主机", width: "150px", render: (r) => h("span", { class: "mono" }, hostWithIp(r.host_ident)) },
      { title: "指标", render: (r) => h("span", { class: "mono" }, r.metric) },
      { title: thLabel("触发值", "触发时的读数：指标类=当时的指标值；探活类=1 表示失败"), width: "80px", render: (r) => h("span", { class: "mono" }, String(r.trigger_value ?? "")) },
      { title: "触发时间", width: "110px", render: (r) => h("span", { class: "muted" }, relTime(r.trigger_time)) },
    ], alerts.slice(0, 8), { emptyText: "当前没有活跃告警", onRowClick: showAlertDrawer })));

  block("接近阈值主机", () => document.getElementById("dash-topcpu").replaceChildren(
    (ov.watch_hosts || []).length
      ? h("div", { class: "topcpu-list" }, ...ov.watch_hosts.map((t) => {
          // 着色与告警同源：已触发=红；逼近(≥90%)=黄；其余=绿
          const cls = t.firing ? "fail" : t.ratio >= 0.9 ? "warn" : "pass";
          const pct = Math.min(100, Math.round(t.ratio * 100));
          return h("div", { class: "topcpu-row clickable", title: `${t.rule_name}：${t.metric} ${t.op}${t.threshold}`,
              onclick: () => openHostDetail(t.ident) },
            h("span", { class: "mono watch-name" }, t.name),
            h("span", { class: "watch-cond mono muted" }, `${t.metric} ${t.op}${t.threshold}`),
            h("div", { class: "topcpu-bar" }, h("i", { class: cls, style: `width:${pct}%` })),
            h("span", { class: `mono num ${cls}` }, String(t.value)));
        }))
      : h("div", { class: "empty" }, "没有接近告警阈值的主机")));

  // 服务健康独立加载（失败只影响本块——沿用 D45 块级隔离纪律）
  drawSvcHealth().catch(() => {});
  drawTodo().catch(() => {});

  timer = setInterval(() => {
    // 抽屉打开时跳过本轮刷新，避免打断阅读；页签不可见时不刷
    if (document.visibilityState === "visible" && !document.querySelector(".detail-panel")) render(root);
  }, REFRESH_S * 1000);
  if (clockTimer) clearInterval(clockTimer);
  clockTimer = setInterval(() => {
    const el = document.getElementById("bj-clock");
    if (el) el.textContent = fmtBeijing(new Date());
  }, 1000);
}

/** D113：待我处理汇总（待确认变更单 / 需人工诊断 / 今日失败任务）。 */
async function drawTodo() {
  const box = document.getElementById("todo-card");
  if (!box) return;
  let d = null;
  try { d = await api.get("/api/ops/todo-counts"); } catch { /* 失败保留空态 */ }
  if (!d) { box.replaceChildren(h("div", { class: "empty" }, "待办统计暂不可用")); return; }
  const items = [
    { k: "待确认变更单", n: d.pending_runbooks || 0, hash: "#/runbooks",
      tip: "AI 已生成、含可执行步骤、等待确认的变更单" },
    { k: "需人工诊断", n: d.needs_human || 0, hash: "#/incidents",
      tip: "最新诊断为 INCONCLUSIVE 且尚无人工订正的事件" },
    { k: "今日失败任务", n: d.failed_tasks_today || 0, hash: "#/runbooks", 
      tip: "今天在目标机执行失败的 Agent 任务（见处置与变更审计）" },
  ];
  const total = items.reduce((s, x) => s + x.n, 0);
  box.replaceChildren(
    h("h3", {}, "待我处理", helpTip("需要人工确认或订正的事项汇总；点数字直达对应页面。")),
    total === 0
      ? h("div", { class: "muted", style: "font-size:0.85rem" }, "没有待处理事项 ✓")
      : h("div", { class: "todo-row" }, ...items.map((x) => h("div", {
          class: "todo-item" + (x.n > 0 ? " hot" : ""),
          title: x.tip,
          onclick: () => { location.hash = x.hash; } },
          h("div", { class: "todo-n" }, String(x.n)),
          h("div", { class: "todo-k" }, x.k)))));
}

function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "k" }, k), h("span", { class: "v mono" }, v ?? "")); }

function showAlertDrawer(a) {
  drawer("告警详情",
    (a.ai_summary || "").trim()
      ? h("div", { class: "ai-brief-item static" },
          h("div", { class: "ai-brief-tag" }, "AI 摘要"), h("p", { html: marked.parse(a.ai_summary) }))   // D127：抽屉里的摘要是第二处漏网
      : h("div", { class: "empty" }, "尚未生成 AI 摘要（可在告警中心触发诊断）"),
    h("div", { class: "kv-grid" },
      kv("规则", a.rule_name || a.rule_id), kv("主机", hostWithIp(a.host_ident)),
      kv("指标", a.metric), kv("触发值", String(a.trigger_value ?? "")),
      kv("级别", `S${a.severity}`), kv("状态", a.status === "ack" ? "已确认" : "触发中"),
      kv("首次触发", fmtTime(a.trigger_time))),
    h("details", { class: "json-fold" },
      h("summary", {}, "原始数据"),
      h("pre", { class: "json-block" }, JSON.stringify(a, null, 2))));
}

function renderSeriesChart(el, series) {
  if (!el) return;
  const c = themeColors();
  const inst = chart(el);
  charts.push(inst);
  const windowDays = curRange.endsWith("d") ? parseInt(curRange, 10) : parseInt(curRange, 10) / 24;
  if (!series.length) {
    inst.setOption({ title: { text: "暂无指标数据（等待 Agent 上报）", left: "center", top: "middle",
      textStyle: { color: c.muted, fontSize: 12, fontWeight: "normal" } } });
    return;
  }
  inst.setOption({
    tooltip: { trigger: "axis", ...tooltipStyle() },
    legend: { data: series.map((s) => s.name), textStyle: { color: c.muted, fontSize: 11 }, top: 0 },
    grid: { left: 46, right: 14, top: 34, bottom: 26 },
    xAxis: { type: "category", boundaryGap: false,
      data: series[0]?.points.map((p) => axisLabel(p[0], windowDays)) || [], ...baseAxis() },
    yAxis: { type: "value", splitNumber: 4, ...baseAxis() },     // D42：刻度更稀疏，不密
    series: series.map((s, i) => ({
      name: s.name, type: "line", smooth: 0.35, showSymbol: false, symbolSize: 5,
      emphasis: { focus: "series" },
      data: s.points.map((p) => p[1]),
      lineStyle: { width: 2, color: i === 0 ? c.accent : c.pass, shadowBlur: 6, shadowColor: c.accentWeak },
      areaStyle: {                                  // D42：两层渐变面积，比纯色块精致
        color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1,
          colorStops: i === 0
            ? [{ offset: 0, color: c.accentSoft }, { offset: 1, color: "transparent" }]
            : [{ offset: 0, color: c.passWeak }, { offset: 1, color: "transparent" }] },
      },
    })),
    color: [c.accent, c.pass, c.warn, c.fail],
  });
}

function renderEventsChart(el, buckets) {
  if (!el) return;
  const c = themeColors();
  const inst = chart(el);
  charts.push(inst);
  const labels = buckets.map((_, i) => {
    const d = new Date(Date.now() - (buckets.length - 1 - i) * 3600 * 1000);
    return `${String(d.getHours()).padStart(2, "0")}:00`;
  });
  inst.setOption({
    tooltip: { trigger: "axis", ...tooltipStyle() },
    grid: { left: 36, right: 14, top: 16, bottom: 26 },
    xAxis: { type: "category", data: labels, ...baseAxis() },
    yAxis: { type: "value", minInterval: 1, splitNumber: 3, ...baseAxis() },
    series: [{ type: "bar", data: buckets, barMaxWidth: 14,
      itemStyle: { borderRadius: [3, 3, 0, 0], opacity: 0.9,
        color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: c.accent }, { offset: 1, color: c.accentRing }] } } }],
  });
}

export function destroy() {
  if (timer) { clearInterval(timer); timer = null; }
  if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
  charts.forEach((c) => c.dispose()); charts = [];
  document.getElementById("content")?.classList.remove("dash-page");
}
