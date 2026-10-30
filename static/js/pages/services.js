// pages/services.js —— 服务监控（P5a，FR-408/409/411）
//
// 这一页回答三个问题：
//   ① 我这些机器上的服务，现在哪个不对劲？（清单 + 三态 + 人话说明）
//   ② 出事了去哪看日志？（一键取证：按时间窗取回相关片段，不是"取多少行"）
//   ③ 平台是不是在自动重启我的服务？（自愈计数与熔断状态——频繁自愈 = 缺陷信号）
import { api, can } from "../api.js";
import { h, table, badge, drawer, toast, select, note, busy, fmtTime, fill, helpTip, kpiRow } from "../ui.js";

const TYPE_LABEL = { mysql: "MySQL", redis: "Redis", nginx: "Nginx", mq: "消息队列", app: "应用进程", other: "其他" };

export async function render(root) {
  const canWrite = can("write");
  const canDiag = can("diag");
  const state = { status: "", host: "" };
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  let killSwitch = false;
  try { killSwitch = (await api.get("/api/ops/heal/kill-switch")).kill_switch; } catch { /* ignore */ }
  const killBtn = h("button", {
    class: killSwitch ? "btn danger" : "btn ghost",
    title: "全局熔断：大故障期间一键停止所有自动恢复（防止重启风暴把故障放大）",
    onclick: async (e) => {
      if (!canWrite) return;
      const next = !killSwitch;
      if (next && !window.confirm("开启全局熔断？所有自愈下发将被拒绝（大故障期间的保护措施）。")) return;
      try {
        const r = await busy(e.currentTarget, "切换中…", () =>
          api.post("/api/ops/heal/kill-switch", { enabled: next }));
        killSwitch = r.kill_switch;
        toast(r.note, killSwitch ? "warn" : "ok");
        draw();
      } catch (err) { toast(`切换失败：${err.message}`, "error"); }
    },
  }, killSwitch ? "自愈已熔断（点击解除）" : "自愈开关：正常");
  const head = h("div", { class: "page-head" },
    h("div", { class: "actions" },
      select([["", "全部状态"], ["down", "不可用"], ["degraded", "异常"], ["up", "正常"], ["unknown", "未知"]],
        "", { onchange: (e) => { state.status = e.target.value; draw(); } }),
      killBtn,
      helpTip("**全局熔断（自愈总开关）**：大故障期间一键停掉**所有**自动恢复动作，防止「重启风暴」把故障放大。开启后恢复动作会被护栏拒绝（仅建议仍可用）；故障处置完记得关回来。"),
      h("button", { class: "btn secondary", onclick: () => { draw(); drawTasks(); } }, "刷新")));
  const summary = h("div", { class: "svc-summary" });
  const tip = note("**服务监控**盯的是「服务对象」（数据库 / 中间件 / 自研应用），而不是只看主机活着——"
    + "判定口径：内置四类按关键指标判（连接数、命中率、队列积压），应用类按**进程 / 端口 / 健康端点**三项判，"
    + "三项全不通=不可用、部分不通=异常。两个动作都在这里：**取日志**是按「告警首次触发时刻」为锚的"
    + "时间窗取证（Agent 在目标机本地筛选后只回相关片段，取不到会明确说明原因，可换窗再取一轮）；"
    + "**恢复**是白名单枚举动作（重启服务/容器、重连连接池、轮转日志），执行前先过九条护栏并给出判决。"
    + "目标机需 Agent ≥ 0.3.0（重跑安装命令即可升级）。");
  const body = h("div", { id: "svc-body" });
  const tasksBox = h("div", { class: "card", style: "margin-top:14px" });
  root.replaceChildren(head, tip, summary, body, tasksBox);

  async function drawSummary() {
    let d = null;
    try { d = await api.get("/api/ops/services/overview"); } catch { /* ignore */ }
    if (!d) { summary.replaceChildren(); return; }
    const b = d.by_status || {};
    fill(summary,
      kpiRow([
        { label: "服务总数", value: d.total, cls: "" },
        { label: "正常", value: b.up || 0, cls: "pass" },
        { label: "异常", value: b.degraded || 0, cls: b.degraded ? "warn" : "" },
        { label: "不可用", value: b.down || 0, cls: b.down ? "fail" : "" },
        { label: "未知", value: b.unknown || 0, cls: "skip" },
      ]),
      (d.unhealthy || []).length ? h("div", { class: "note", style: "margin-top:10px" },
        "需要关注：" + d.unhealthy.slice(0, 5).map((s) => `${s.host} / ${s.name}（${s.status === "down" ? "不可用" : "异常"}）`).join("；")
        + (d.unhealthy.length > 5 ? ` 等 ${d.unhealthy.length} 项` : "")) : null);
  }


  async function draw() {
    body.replaceChildren(h("div", { class: "empty" }, "加载中…"));
    let d = { items: [] };
    try {
      d = await api.get("/api/ops/services", { status: state.status, host: state.host });
    } catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    const items = d.items || [];
    body.replaceChildren(table([
      { title: "序号", width: "46px", render: (_r, i) => h("span", { class: "mono muted" }, String(i + 1)) },   // D129
      { title: "服务", render: (r) => h("div", { class: "svc-name" },
          h("span", {}, r.name), h("span", { class: "muted svc-type" }, TYPE_LABEL[r.type] || r.type)) },
      { title: "主机", width: "140px", render: (r) => h("span", { class: "mono", style: "font-size:0.78rem" }, r.host) },
      { title: ["状态", helpTip("正常 / 异常 / 不可用 / 未知 四态。内置四类（MySQL / Redis / Nginx / 消息队列）按关键指标判；应用类按**进程 / 端口 / 健康端点**三项判：三项全不通=不可用，部分不通=异常。")], width: "96px", render: (r) => badge(
          r.status === "up" ? "正常" : r.status === "degraded" ? "异常" : r.status === "down" ? "不可用" : "未知",
          r.status === "up" ? "PASS" : r.status === "degraded" ? "WARN" : r.status === "down" ? "FAIL" : "SKIP") },
      { title: "说明 / 关键指标", render: (r) => h("div", { class: "svc-note" },
          h("div", { class: "muted" }, r.note || "-"),
          h("div", { class: "svc-metrics mono muted" }, metricText(r))) },
      { title: "最近检查", width: "110px", render: (r) => h("span", { class: "muted", style: "font-size:0.76rem" }, fmtTime(r.last_check_at)) },
      { title: ["操作", helpTip("取日志：按「告警首次触发时刻」为锚的时间窗取证（Agent 在目标机本地筛错、折叠重复，只回相关片段）。恢复：白名单内的枚举动作，执行前先过九条护栏。改声明：填日志路径与单元名/容器名，改完立即生效、不会被 Agent 心跳覆盖。")], width: "246px", render: (r) => h("div", { class: "row-actions" },
          canDiag ? h("button", { class: "btn ghost", onclick: () => fetchLog(r) }, "取日志") : null,
          // 恢复按钮**始终可见**（含正常状态）：健康服务的恢复会被幂等护栏拒绝并解释原因——
          // 藏起来会让"我想重启一下"的用户找不到入口，也看不到护栏为什么不让动。
          canDiag ? h("button", { class: "btn ghost", onclick: () => healDialog(r) }, "恢复") : null,
          h("button", { class: "btn ghost", onclick: () => detail(r) }, "详情"),
          canWrite ? h("button", { class: "btn ghost", onclick: () => editSpec(r) }, "改声明") : null) },
    ], items, { emptyText: "暂无服务——Agent 会自动发现本机的 MySQL/Redis/Nginx/MQ；"
      + "自研应用需在接入向导里声明（或在服务行点「改声明」补充日志路径）" }));
  }

  function metricText(r) {
    const m = r.metrics || {};
    if (r.type === "mysql" && m.conn_used_ratio != null) {
      return `连接 ${m.Threads_connected}/${m.max_connections}（${Math.round(m.conn_used_ratio * 100)}%）` +
             (m.Slow_queries != null ? ` · 慢查询 ${m.Slow_queries}` : "");
    }
    if (r.type === "redis") {
      const bits = [];
      if (m.used_memory != null) bits.push(`内存 ${(m.used_memory / 1048576).toFixed(0)}MB`);
      if (m.hit_rate != null) bits.push(`命中率 ${Math.round(m.hit_rate * 100)}%`);
      if (m.connected_clients != null) bits.push(`客户端 ${m.connected_clients}`);
      return bits.join(" · ");
    }
    if (r.type === "nginx") return `活跃连接 ${m.active_connections ?? "-"} · 请求 ${m.requests ?? "-"}`;
    if (r.type === "mq") return `队列 ${m.queues ?? "-"} · 最大积压 ${m.max_queue_depth ?? "-"}`;
    if (r.type === "app") {
      const bits = [];
      if (m.process_ok != null) bits.push(m.process_ok ? "进程存活" : "进程不可用");
      if (m.port_ok != null) bits.push(m.port_ok ? "端口可连" : "端口不可连");
      if (m.health_ok != null) bits.push(m.health_ok ? "健康端点正常" : "健康端点异常");
      return bits.join(" · ");
    }
    return "";
  }

  /** 一键取证：**按时间窗**取（默认以"现在"为锚前 5 分钟；可从事件详情带 anchor 进来）。 */
  function fetchLog(svc, anchor) {
    const winMin = select([["5", "触发前 5 分钟"], ["15", "前 15 分钟"], ["30", "前 30 分钟"], ["60", "前 1 小时"]], "5");
    const pat = h("input", { class: "vs-input mono", placeholder: "可选：关键词/正则，用于聚焦（如 Too many / timeout）" });
    const ctx = select([["3", "前后 3 行"], ["6", "前后 6 行"], ["10", "前后 10 行"]], "3");
    drawer(`取日志 · ${svc.name}`,
      note("取证是**按时间窗**取回**相关片段**，不是「取多少行」：Agent 会在目标机本地按级别/错误签名筛选、"
        + "折叠重复行、保留堆栈与首错，只把与故障相关的部分传回来。取回量通常只有原文的百分之几。"),
      h("div", { class: "form-row" }, h("label", {}, "时间窗（以告警首次触发时刻为锚，更贴近成因）", helpTip("锚点是**告警首次触发时刻**而不是现在：故障常发生在触发之前，按 now 取尾部会整段错过成因。默认前 5 分钟 ~ 后 2 分钟。第一轮不够可以换窗再取（每事件 ≤5 次、≤1MB）。")), winMin),
      h("div", { class: "form-row" }, h("label", {}, "上下文行数", helpTip("每个命中错误前后各带几行，便于看清现场。堆栈帧与异常续行不受这个限制——它们始终完整保留（半截堆栈会误导分析）。")), ctx),
      h("div", { class: "form-row" }, h("label", {}, "聚焦关键词（可选）", helpTip("给一个错误里的词（如 Too many / timeout）让 Agent 只捞相关片段。不知道填什么就留空：不填时按级别 + 错误签名自动筛。")), pat),
      h("div", { class: "muted", style: "font-size:0.78rem" },
        `目标文件：${(svc.spec || {}).log_path || "（未声明，需在「改声明」里填）"}` +
        ((svc.spec || {}).unit ? ` · systemd 单元：${svc.spec.unit}` : "")),
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        try {
          const r = await busy(e.currentTarget, "已下发，等 Agent 执行…", () =>
            api.post(`/api/ops/services/${svc.id}/log-fetch`, {
              window_min: Number(winMin.value), pattern: pat.value.trim(),
              context: Number(ctx.value), anchor: anchor || "", origin: anchor ? "incident" : "" }));
          toast("取证已下发（Agent 约 15 秒内执行）", "ok");
          document.querySelector(".detail-panel .detail-head .btn")?.click();
          await pollTask(r.task_id, "log_fetch");
        } catch (err) { toast(`下发失败：${err.message}`, "error"); }
      } }, "下发取证")));
  }

  /** 轮询任务结果（最多等 60 秒）。kind 分路：取证看日志，动作看执行与后置校验——
      两类任务的字段完全不同，混用会把动作面板渲染成"扫描行数 -"的日志面板。 */
  async function pollTask(tid, kind = "log_fetch") {
    const isAct = kind === "svc_action";
    const head = isAct ? `恢复动作 #${tid}` : `日志取证 #${tid}`;
    const box = h("div", { class: "card" }, h("h3", {}, `${isAct ? "恢复" : "取证"}任务 #${tid}`),
      h("div", { class: "empty" }, "等待 Agent 执行…"));
    drawer(head, box);
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      let t = null;
      try { t = await api.get(`/api/ops/tasks/${tid}`); } catch { /* 继续等 */ }
      if (!t) continue;
      if (t.status === "done") {
        if (isAct) { renderAction(box, t); return; }
        if ((t.result || {}).ok) { renderLog(box, t); return; }
        box.replaceChildren(h("h3", {}, `取证任务 #${tid}`),
          h("div", { class: "note" }, `取证未成功：${(t.result || {}).error || t.error || "未知原因"}`),
          h("div", { class: "muted" }, "常见原因：日志路径未声明 / 路径不在白名单 / 目标机 Agent 版本过低"));
        return;
      }
      if (t.status === "failed" || t.status === "expired") {
        box.replaceChildren(h("h3", {}, `${isAct ? "恢复" : "取证"}任务 #${tid}`),
          h("div", { class: "note" }, `${isAct ? "动作执行" : "取证"}未成功：${t.error || "未知原因"}`),
          h("div", { class: "muted" }, isAct
            ? "常见原因：目标机 Agent 离线 / 服务名与 systemd 单元不一致 / 动作被目标机拒绝"
            : "常见原因：日志路径未声明 / 路径不在白名单 / 目标机 Agent 版本过低"));
        return;
      }
      box.lastChild.textContent = `等待 Agent 执行…（已等 ${(i + 1) * 2}s）`;
    }
    box.replaceChildren(h("h3", {}, `${isAct ? "恢复" : "取证"}任务 #${tid}`),
      h("div", { class: "note" }, "Agent 未在 60 秒内完成——主机可能已离线（关机/断网/Agent 停止），"
        + "可在「主机管理」确认状态；残留任务会在 5 分钟后自动回收，主机恢复后可重试。"));
  }

  /** 渲染恢复动作结果：执行成败 + 原始输出 + 后置校验（FR-411 的可见落点）。 */
  function renderAction(box, t) {
    const r = t.result || {};
    const pl = t.payload || {};
    const pc = r.post_check || {};
    const checks = pc.checks || [];
    fill(box,
      h("h3", {}, `恢复结果 · 任务 #${t.id}`),
      h("div", { class: "detail-meta" },
        kv("动作", h("span", { class: "mono" }, pl.action || "-")),
        kv("目标服务", h("span", { class: "mono" }, pl.service || "-")),
        kv("执行结果", badge(r.ok ? "成功" : r.denied ? "被拒绝" : "失败",
          r.ok ? "PASS" : r.denied ? "WARN" : "FAIL")),
        r.elapsed_ms !== undefined ? kv("耗时", h("span", { class: "mono" }, `${r.elapsed_ms} ms`)) : null,
        r.rc !== undefined ? kv("返回码", h("span", { class: "mono" }, String(r.rc))) : null),
      r.error ? h("div", { class: "note" }, `说明：${r.error}`) : null,
      checks.length ? h("div", { class: "card" }, h("h3", {}, "后置校验（任一通过即视为恢复）"),
        h("div", { class: "detail-meta" }, ...checks.map((c) => kv({
          systemd_active: "systemd is-active", port: "端口可连", process: "进程存活",
        }[c.name] || c.name, badge(c.ok ? "通过" : "未通过", c.ok ? "PASS" : "FAIL"))))) : null,
      (r.stdout || r.stderr) ? h("div", { class: "card" }, h("h3", {}, "原始输出"),
        h("pre", { class: "log-pre" }, `${r.stdout || ""}${r.stderr ? (r.stdout ? "\n" : "") + r.stderr : ""}`.trim()
          || "（无输出）")) : null,
      r.ok && checks.length && !pc.ok
        ? h("div", { class: "note" }, "动作命令成功但后置校验全部未通过——服务可能还没起稳，"
            + "建议 30 秒后复查状态；连续 2 次失败会自动熔断，转人工处理。") : null);
  }

  function renderLog(box, t) {
    const r = t.result || {};
    const w = r.window || {};
    box.replaceChildren(
      h("h3", {}, `取证结果 · 任务 #${t.id}`),
      h("div", { class: "detail-meta" },
        kv("时间窗", h("span", { class: "mono", style: "font-size:0.76rem" },
          `${String(w.since || "").slice(5, 19)} → ${String(w.until || "").slice(5, 19)}`)),
        kv("定位方式", badge({ journalctl: "journald", timestamp: "行首时间戳", keyword: "关键词兜底" }[w.source] || w.source || "-",
          w.source === "keyword" ? "WARN" : "PASS")),
        kv("扫描行数", h("span", { class: "mono" }, String(r.scanned ?? "-"))),
        kv("窗口内命中", h("span", { class: "mono" }, String(r.matched ?? "-"))),
        kv("折叠重复", h("span", { class: "mono" }, `${r.deduped ?? 0} 条`)),
        kv("丢弃噪声", h("span", { class: "mono" }, `${r.dropped_by_level ?? 0} 行`)),
        kv("回传体积", h("span", { class: "mono" }, `${r.bytes ?? 0} 字节${r.truncated ? "（已截断）" : ""}`))),
      (r.segments || []).length
        ? h("div", { class: "muted", style: "font-size:0.76rem;margin:8px 0" },
            "取回的是筛选后的相关片段（按签名分段、重复已折叠、堆栈完整保留）——这就是给 AI 读的证据。")
        : h("div", { class: "note", style: "margin:8px 0" },
            `窗口内没有取到错误日志。${r.hint || ""}` +
            "（可换更宽的时间窗、换个文件或加关键词再取一轮——取证本就是可迭代的）"),
      ...(r.segments || []).map((seg) => h("div", { class: "card log-seg" },
        h("div", { class: "log-seg-head" },
          h("span", { class: "mono" }, `[${seg.signature}]`),
          h("span", { class: "muted" }, `命中 ${seg.count} 次`),
          seg.repeats ? h("span", { class: "muted" }, `· 已折叠重复 ${seg.repeats} 条`) : null,
          seg.first_ts ? h("span", { class: "muted mono" }, `· 首见 ${String(seg.first_ts).slice(5, 19)}`) : null),
        h("pre", { class: "log-pre" }, (seg.lines || []).join("\n")))));
  }

  function editSpec(s) {
    const f = {
      port: h("input", { class: "vs-input mono", type: "number", value: (s.spec || {}).port || "" }),
      unit: h("input", { class: "vs-input mono", value: (s.spec || {}).unit || "", placeholder: "systemd 单元名（可选）" }),
      container: h("input", { class: "vs-input mono", value: (s.spec || {}).container || "", placeholder: "容器名（可选，restart_container 用它）" }),
      log_path: h("input", { class: "vs-input mono", value: (s.spec || {}).log_path || "", placeholder: "/var/log/...（取证按它取）" }),
      process: h("input", { class: "vs-input mono", value: (s.spec || {}).process || "", placeholder: "进程名关键字（应用类）" }),
      health_url: h("input", { class: "vs-input mono", value: (s.spec || {}).health_url || "", placeholder: "http://127.0.0.1:8080/health（可选）" }),
    };
    drawer(`改服务声明 · ${s.name}`,
      note("**平台侧声明优先**：这里改的值不会被 Agent 心跳覆盖。日志路径尤其重要——取证按它取。"
        + "恢复动作的目标取自**单元名/容器名**（不会拿服务显示名去猜）。"),
      h("div", { class: "grid-2" }, frow("端口", f.port), frow(["systemd 单元", helpTip("恢复动作「重启服务 / 重连连接池 / 轮转日志」的目标名（systemd 单元名）；也为取证提供 journald 兜底。不填这些动作会被护栏拒绝。")], f.unit)),
      h("div", { class: "grid-2" }, frow(["容器名", helpTip("恢复动作「重启容器」的目标（docker 容器名）。")], f.container), frow("进程名关键字（应用类）", f.process)),
      frow(["日志路径", helpTip("**取证按它取**，是这里最重要的一项。不填就只能靠 systemd 单元走 journald；填错时取证会明确告诉你「文件不存在」而不是悄悄返回空。")], f.log_path), frow("健康端点（可选）", f.health_url),
      h("div", { class: "detail-actions" },
        h("button", { class: "btn", onclick: async (e) => {
          try {
            await busy(e.currentTarget, "保存中…", () => api.patch(`/api/ops/services/${s.id}`, {
              port: f.port.value ? Number(f.port.value) : null, unit: f.unit.value,
              container: f.container.value, log_path: f.log_path.value,
              process: f.process.value, health_url: f.health_url.value }));
            toast("已保存", "ok"); document.querySelector(".detail-panel .detail-head .btn")?.click(); draw();
          } catch (err) { toast(`保存失败：${err.message}`, "error"); }
        } }, "保存"),
        canWrite ? h("button", { class: "btn ghost danger", onclick: async (e) => {
          if (!window.confirm(`删除服务「${s.name}」？`)) return;
          try { await busy(e.currentTarget, "删除中…", () => api.del(`/api/ops/services/${s.id}`));
            toast("已删除", "ok"); document.querySelector(".detail-panel .detail-head .btn")?.click(); draw(); }
          catch (err) { toast(`删除失败：${err.message}`, "error"); }
        } }, "删除") : null));
  }

  /** 详情：指标明细 + 自愈计数/熔断（FR-411：频繁自愈要被看见）。 */
  async function detail(s) {
    let st = null;
    try { st = await api.get(`/api/ops/services/${s.id}/heal-state`); } catch { /* ignore */ }
    const rows = Object.entries(s.metrics || {}).map(([k, v]) => ({ k, v }));
    drawer(`服务详情 · ${s.name}`,
      h("div", { class: "detail-meta" },
        kv("状态", badge(s.status === "up" ? "正常" : s.status === "degraded" ? "异常" : s.status === "down" ? "不可用" : "未知",
          s.status === "up" ? "PASS" : s.status === "degraded" ? "WARN" : s.status === "down" ? "FAIL" : "SKIP")),
        kv("类型", TYPE_LABEL[s.type] || s.type),
        kv("主机", s.host), kv("最近检查", fmtTime(s.last_check_at)),
        kv("端口", String((s.spec || {}).port || "-")),
        kv("日志路径", h("span", { class: "mono", style: "font-size:0.76rem" }, (s.spec || {}).log_path || "未声明")),
        // P2：AI 取证自动发现到的日志路径（来自进程打开的文件）——一键采纳转正
        (!(s.spec || {}).log_path && (s.spec || {}).suggested_log_path)
          ? kv("建议日志路径", h("span", { class: "mono", style: "font-size:0.76rem" },
              (s.spec || {}).suggested_log_path, " ",
              canWrite ? h("button", { class: "btn ghost sm", title: "把它设为该服务的日志路径（取证按它取）",
                onclick: async (e) => {
                  try {
                    await busy(e.currentTarget, "采纳中…", () =>
                      api.patch(`/api/ops/services/${s.id}`, { log_path: (s.spec || {}).suggested_log_path }));
                    toast("已采纳建议日志路径", "ok");
                    detail(s);
                  } catch (err) { toast(`采纳失败：${err.message}`, "error"); }
                } }, "采纳") : null))
          : null),
      h("div", { class: "note" }, s.note || "-"),
      rows.length ? h("div", { class: "card" }, h("h3", {}, "关键指标"), table([
        { title: "指标", render: (r) => h("span", { class: "mono" }, r.k) },
        { title: "值", width: "150px", render: (r) => h("span", { class: "mono" }, String(r.v)) },
      ], rows, {})) : null,
      st ? h("div", { class: "card" }, h("h3", {}, `自愈情况（近 ${st.days} 天）`),
        st.frequent ? note("注意：该服务近期被自愈 ≥3 次——**频繁自愈通常是缺陷信号**，"
          + "建议排查根因而不是依赖自动重启（「每晚被重启的服务永远不会被修好」）。") : null,
        st.tripped ? h("div", { class: "note" }, `已熔断：${st.tripped_reason}`) : null,
        h("div", { class: "detail-meta" },
          kv("自愈次数", String(st.total)), kv("成功", String(st.succeeded)), kv("失败", String(st.failed)),
          kv("连续失败", String(st.fail_streak))),
        canWrite && st.tripped ? h("div", { class: "detail-actions" },
          h("span", { class: "tip-inline" },
            h("button", { class: "btn secondary", onclick: async (e) => {
              try { await busy(e.currentTarget, "重置中…", () => api.post(`/api/ops/services/${s.id}/heal-reset`, {}));
                toast("已重置熔断（恢复自动执行）", "ok"); detail(s); }
              catch (err) { toast(`重置失败：${err.message}`, "error"); }
            } }, "重置熔断（确认已修好根因）"),
            helpTip("连续 2 次后置校验未通过会自动熔断、停止自动执行并转人工。确认已经修好根因（而不是靠重启续命）后在这里重置。"))) : null) : null,
      h("div", { class: "detail-actions" },
        canDiag ? h("button", { class: "btn", onclick: () => { document.querySelector(".detail-panel .detail-head .btn")?.click(); fetchLog(s); } }, "取日志") : null,
        canWrite ? h("button", { class: "btn secondary", onclick: () => { document.querySelector(".detail-panel .detail-head .btn")?.click(); editSpec(s); } }, "改声明") : null));
  }

  /** P5c（FR-411）：恢复动作对话框 —— 三档开关 + 护栏判决预览 + 演练。 */
  async function healDialog(svc) {
    let actions = [], limits = {};
    try { const d = await api.get("/api/ops/heal/actions"); actions = d.items || []; limits = d.limits || {}; } catch { /* ignore */ }
    const act = select(actions.map((a) => [a.action, a.label]), actions.length ? actions[0].action : "restart_service");
    const mode = select([["suggest", "仅建议（默认，不下发）"], ["manual", "人工确认后执行"], ["auto", "白名单自动"]], "suggest");
    const pre = h("div", { class: "note" });
    const req = h("div", { class: "muted", style: "font-size:0.76rem" });
    const warn = h("div", { class: "muted", style: "font-size:0.78rem" });

    async function preview() {
      try {
        // 预览是只读判决（GET）——不产生动作，也就不需要防重复提交守卫
        const v = await api.get(`/api/ops/services/${svc.id}/heal/preview`, { action: act.value });
        pre.replaceChildren(h("b", {}, v.allow ? "护栏通过 ✓" : `护栏拒绝（${v.rule}）`), " ", v.reason);
        pre.className = v.allow ? "note" : "note";
        req.textContent = v.requirement ? `本动作要求：${v.requirement}` : "";
        warn.textContent = (v.warnings || []).join("；");
        lastVerdict = v;
      } catch (e) { pre.textContent = `护栏预览失败：${e.message}`; }
      syncBtn();
    }
    let lastVerdict = null;
    act.addEventListener("change", preview);

    /* 按钮语义必须跟着"执行方式"走（用户实测反馈：默认「仅建议」下点「执行」只弹一句
       "护栏判决如下（未下发）"，看不出自己是不是做错了什么——按钮名叫执行，实际什么都没执行，
       是误导）。另外护栏已拒绝时直接禁用按钮，而不是让人点了再看一句模糊提示。 */
    const goBtn = h("button", { class: "btn", onclick: async (e) => {
      const m = mode.value;
      const v = lastVerdict || {};
      if (m === "suggest") {
        // 仅建议：只重跑判决，明确说明"这是建议、没有下发"，并告诉他要下发该怎么做
        toast("仅建议（未下发）：已按护栏判决。要真正执行，请把「执行方式」改为「人工确认后执行」或「白名单自动」",
          v.allow ? "ok" : "warn");
        await preview();
        return;
      }
      if (m === "manual" && !window.confirm(`确认以「人工确认」方式执行「${act.value}」？`)) return;
      try {
        const r = await busy(e.currentTarget, "已下发…", () =>
          api.post(`/api/ops/services/${svc.id}/heal`, { action: act.value, mode: m, confirmed: m === "manual" }));
        if (!r.dispatched) { await preview(); return; }
        toast(`已下发（后置校验：${r.post_check}）`, "ok");
        document.querySelector(".detail-panel .detail-head .btn")?.click();
        pollTask(r.task_id, "svc_action");
      } catch (err) { toast(`${err.message}`, "error"); }
    } }, "执行");
    const syncBtn = () => {
      const m = mode.value;
      const v = lastVerdict || {};
      goBtn.textContent = m === "suggest" ? "查看判决（不下发）" : (m === "manual" ? "执行（需确认）" : "执行（白名单自动）");
      goBtn.disabled = m !== "suggest" && v.allow === false;
      goBtn.title = goBtn.disabled
        ? `护栏已拒绝（${v.rule}）：${v.reason || ""}——先按上面的提示处理`
        : (m === "suggest" ? "仅建议：只跑护栏判决，不下发任何动作" : "护栏通过后下发到目标机执行");
      goBtn.style.opacity = goBtn.disabled ? "0.55" : "1";
    };
    mode.addEventListener("change", syncBtn);
    await preview();          // 打开即跑一次判决（preview 内部会刷新按钮状态）

    drawer(`恢复 · ${svc.name}`,
      note("恢复动作**只认白名单内的枚举动作**（无自由文本命令）。执行前经九条护栏判决："
        + "限流、连续失败熔断、爆炸半径、依赖感知、无校验不执行、幂等、发布窗口互斥、全局熔断、自愈计数。"),
      h("div", { class: "grid-2" }, frow(["动作", helpTip("只认白名单内的枚举动作（无自由文本命令）：重启服务 / 重启容器 / 重连连接池 / 轮转日志。每个动作都要有对应声明才动得了：服务类要 systemd 单元名，容器类要容器名。")], act), frow(["执行方式", helpTip("仅建议=只跑护栏判决、**不下发**；人工确认=你确认后才下发；白名单自动=护栏全通过即下发（仍受限流、熔断、依赖、发布窗口约束）。")], mode)),
      h("div", { class: "form-row" }, h("label", {}, "护栏判决"), pre),
      req,
      warn,
      h("div", { class: "muted", style: "font-size:0.76rem" },
        `限额：每主机每时 ≤${limits.per_host_per_hour || 3} 次 · 连续 ${limits.fail_streak_trip || 2} 次校验失败即熔断 · `
        + `近 ${limits.frequent_window_days || 7} 天自愈 ≥${limits.frequent_trip || 3} 次会提示改人工修根因`),
      h("div", { class: "detail-actions" }, goBtn));
  }

  async function drawTasks() {
    let d = { items: [] };
    try { d = await api.get("/api/ops/tasks/recent", { limit: 6 }); } catch { /* ignore */ }
    const items = d.items || [];
    tasksBox.replaceChildren(
      h("h3", {}, "最近取证 / 动作"),
      items.length ? table([
        { title: "#", width: "54px", render: (r) => h("span", { class: "mono muted" }, String(r.id)) },
        { title: "类型", width: "92px", render: (r) => badge(r.kind === "log_fetch" ? "取日志" : "动作", "ACCENT") },
        { title: "主机", width: "130px", render: (r) => h("span", { class: "mono", style: "font-size:0.78rem" }, r.host) },
        { title: "结果", render: (r) => h("span", { class: r.result_ok ? "" : "muted" }, r.summary || "-") },
        { title: "时间", width: "150px", render: (r) => h("span", { class: "muted mono", style: "font-size:0.76rem" }, fmtTime(r.created_at)) },
        { title: "", width: "76px", render: (r) => h("button", { class: "btn ghost", onclick: () => pollTask(r.id, r.kind) }, "查看") },
      ], items, {}) : h("div", { class: "muted" }, "暂无取证记录（在服务行点「取日志」发起）"));
  }

  function frow(label, el) { return h("div", { class: "form-row" }, h("label", {}, label), el); }
  function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v)); }

  await drawSummary();
  await draw();
  await drawTasks();
}

export function destroy() {}
