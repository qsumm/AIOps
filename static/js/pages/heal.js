// pages/heal.js —— 自动自愈策略（D140 用户需求）
//
// 用户场景：「集群平均 CPU 超阈值 → 自动补机器拉代码上线」「数据库连接数满 / CPU>90% →
// 自动 kill 进程」「磁盘>90% → 自动扩容/清理」——平台落法是**策略引擎**：
//   触发条件（关联告警规则或直填指标）→ 冷却 → 动作白名单（重启服务/重启容器/重连连接池/
//   轮转日志）→ 九条护栏（与人工 heal 完全同源）→ 三档执行（仅建议/人工确认/自动）。
// 每次触发都留痕（执行日志页签），自愈不是黑盒。
//
// 诚实边界（页面文案明说，不装能做）：k8s 扩容 / 云盘扩容需要对接云 API，当前动作
// 白名单没有——对应场景用「重连连接池 / 轮转日志」缓解，或走后续轮次对接云 API。
import { api, can } from "../api.js";
import { h, table, badge, statusBadge, drawer, toast, select, note, fmtTime, relTime,
         busy, reqLabel, needText, helpTip, pager, svgIcon } from "../ui.js";

const STATUS_META = {
  dry_run:         ["演练", "SKIP",    "仅评估未执行——护栏结论在详情里"],
  waiting_confirm: ["待确认", "WARN",   "人工确认档：护栏已通过，等你点「确认执行」"],
  dispatched:      ["已执行", "PASS",   "护栏通过并已下发到 Agent 执行"],
  denied:          ["被拦截", "FAIL",   "护栏拒绝——原因在详情里，不是出了故障就是配置不对"],
  failed:          ["失败",   "FAIL",   "下发失败（Agent 掉线/限流），详情里有原因"],
  predicted:       ["预测命中", "WARN", "趋势外推命中——已全渠道通知，请提前处理（预测不自动执行）"],
};
const MODE_META = {
  suggest: ["仅建议", "SKIP"],
  manual:  ["人工确认", "WARN"],
  auto:    ["自动执行", "ACCENT"],
};
const METRIC_PRESETS = [
  ["cpu.percent", "CPU 使用率"],
  ["mem.percent", "内存使用率"],
  ["disk.percent", "磁盘使用率"],
  ["load.1", "1 分钟负载"],
];

let es = null;

export async function render(root) {
  const canWrite = can("write");
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  const head = h("div", { class: "page-head" },
    h("div", { class: "actions" },
      h("button", { class: "btn secondary", onclick: draw }, "刷新"),
      h("button", { class: "btn secondary", onclick: () => runs(null) }, "执行日志"),
      canWrite ? h("button", { class: "btn", onclick: () => editor(null) }, "新建自愈策略") : null));
  const tip = note(
    "告警触发 → 自动执行白名单动作（重启服务 / 重连连接池 / 轮转日志），执行前过**与人工恢复相同的护栏**。"
    + "三档：仅建议（留痕）/ 人工确认 / 自动执行。k8s 扩容、云盘扩容需对接云 API，暂不在白名单内。");
  const pagerEl = h("div", { class: "pager" });
  const body = h("div", { id: "heal-body" });
  const state = { page: 1, pageSize: 20 };
  root.replaceChildren(head, tip, body, pagerEl);

  async function draw() {
    let d;
    try { d = await api.get("/api/ops/heal-policies"); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    const items = d.items || [];
    if (!items.length) {
      body.replaceChildren(h("div", { class: "empty" },
        "还没有自愈策略——点右上「新建自愈策略」，或先到「告警规则」把触发条件配好"));
      pagerEl.replaceChildren();
      return;
    }
    body.replaceChildren(table([
      { title: "序号", width: "46px", render: (_r, i) => h("span", { class: "mono muted" }, String(i + 1)) },
      { title: "策略", render: (r) => h("div", {},
          h("div", {}, h("span", { style: "font-weight:600" }, r.name),
            r.enabled ? null : h("span", { class: "badge SKIP", style: "margin-left:8px" }, "已停用")),
          h("div", { class: "muted", style: "font-size:0.76rem;margin-top:2px" },
            r.trigger_rule_id ? `触发：关联告警规则 #${r.trigger_rule_id}`
              : `触发：${r.trigger_metric} ${r.trigger_op} ${r.trigger_threshold}（级别 S${r.trigger_severity}）`)) },
      { title: "动作", width: "130px", render: (r) => h("span", { class: "mono" }, r.action_label) },
      { title: ["执行档", helpTip("仅建议=只评估留痕不下发；人工确认=护栏通过后等你点确认；自动执行=护栏通过即下发（生产慎用）")],
        width: "100px", render: (r) => badge(...(MODE_META[r.mode] || [r.mode, "SKIP"])) },
      { title: "冷却", width: "70px", render: (r) => h("span", { class: "mono muted" }, `${r.cooldown_min}min`) },
      { title: "预测", width: "64px", render: (r) => r.predict_min > 0
          ? h("span", { class: "badge ACCENT", title: `提前 ${r.predict_min} 分钟趋势外推预警（只通知不动手）` }, `${r.predict_min}min`)
          : h("span", { class: "muted" }, "-") },
      { title: "最近状态", width: "170px", render: (r) => {
          const meta = STATUS_META[r.last_status] || [r.last_status, "SKIP", ""];
          return h("div", {},
            badge(meta[0], meta[1]),
            h("div", { class: "muted", style: "font-size:0.72rem;margin-top:2px" },
              r.last_fired_at ? relTime(r.last_fired_at) : "从未触发"));
        } },
      canWrite ? { title: "操作", width: "230px", render: (r) => h("div", { class: "row-actions" },
          h("button", { class: "btn ghost", title: "立即以当前状态跑一遍护栏（不下发）",
            onclick: (e) => dryRun(r, e.currentTarget) }, "演练"),
          h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); editor(r); } }, "编辑"),
          h("button", { class: "btn ghost", title: r.enabled ? "停用后不再触发" : "重新启用",
            onclick: (e) => { e.stopPropagation(); toggle(r, e.currentTarget); } },
            r.enabled ? h("span", { style: "color:var(--warn)" }, "停用") : h("span", { style: "color:var(--pass)" }, "启用")),
          h("button", { class: "btn ghost danger", onclick: (e) => { e.stopPropagation(); del(r, e.currentTarget); } }, "删除")) } : null,
    ].filter(Boolean), items, { onRowClick: (r) => runs(r) }));
    pagerEl.replaceChildren();
  }

  // ── 执行日志抽屉（点策略行 / 「日志」入口）──
  async function runs(p) {
    let d;
    const q = p ? `policy_id=${p.id}&` : "";
    try { d = await api.get(`/api/ops/heal-policy-runs?${q}page_size=50`); }
    catch (e) { toast(`加载失败：${e.message}`, "error"); return; }
    const items = d.items || [];
    drawer(`执行日志${p ? " · " + p.name : " · 全部策略"}`,
      note("每次触发都留痕：**发生了什么 / 护栏怎么说 / 做了什么**。待确认的条目可在这里确认执行"
        + "（确认时护栏会**重新评估**——留痕到执行之间世界可能已经变了）。"),
      items.length ? table([
        { title: "时间", width: "140px", render: (r) => h("span", { class: "muted mono", style: "font-size:0.74rem" }, fmtTime(r.created_at)) },
        { title: "触发值", width: "120px", render: (r) => h("span", { class: "mono" }, r.metric ? `${r.metric}=${r.metric_value ?? "-"}` : "-") },
        { title: "状态", width: "90px", render: (r) => {
            const meta = STATUS_META[r.status] || [r.status, "SKIP", ""];
            return h("span", { class: `badge ${meta[1]}`, title: meta[2] }, meta[0]);
          } },
        { title: "说明", render: (r) => h("div", { style: "font-size:0.78rem;line-height:1.5" },
            r.guard_reason ? h("div", { class: "muted" }, r.guard_reason) : null,
            r.detail && r.detail.note ? h("div", { class: "muted" }, r.detail.note) : null,
            r.task_id ? h("div", {}, h("a", { class: "cite mono", href: "javascript:void(0)",
              onclick: () => taskDetail(r.task_id) }, `任务 #${r.task_id}（点看执行结果）`)) : null) },
        // D146：这里原先写的是 `canWrite && r.status === ...`——**列定义里根本没有 r**
        // （r 是每行的渲染参数），一进 runs() 就抛 ReferenceError: r is not defined
        // → 「执行日志」抽屉**永远打不开**（点行、点「执行日志」都是白点）。
        // 正确写法：按**是否有待确认的行**决定要不要这一列，条件放到 render 里。
        (canWrite && items.some((x) => x.status === "waiting_confirm"))
          ? { title: "操作", width: "110px", render: (r) => (r.status === "waiting_confirm"
              ? h("button", { class: "btn secondary",
                onclick: (e) => confirmRun(r, e.currentTarget) }, "确认执行")
              : "") }
          : null,
      ].filter(Boolean), items, { emptyText: "该策略还没有触发记录" })
        : h("div", { class: "empty" }, "该策略还没有触发记录——触发条件命中后这里会留下完整轨迹"));
  }

  /** 任务执行详情（Agent 真实回传）：状态 + 结果，自愈做了什么一目了然。 */
  async function taskDetail(tid) {
    let d;
    try { d = await api.get(`/api/ops/tasks/${tid}`); }
    catch (e) { toast(`加载失败：${e.message}`, "error"); return; }
    const t = d.data || d;
    const statusMeta = { pending: ["等待执行", "SKIP"], picked: ["执行中", "RUNNING"],
      done: ["已完成", "PASS"], failed: ["失败", "FAIL"], expired: ["已过期", "FAIL"] };
    const m = statusMeta[t.status] || [t.status, "SKIP", ""];
    const res = t.result || {};
    drawer(`任务 #${tid} · ${t.kind === "svc_action" ? "自愈动作执行" : t.kind}`,
      h("div", { class: "detail-meta" },
        kv("状态", h("span", { class: `badge ${m[1]}` }, m[0])),
        kv("动作", h("span", { class: "mono" }, t.payload?.action || "-")),
        kv("目标", h("span", { class: "mono" }, t.payload?.service || "-")),
        kv("后置校验", t.payload?._post ? h("span", { class: "muted" }, t.payload._post.mode === "systemctl" ? "systemd 状态 / 端口" : "容器运行 / 端口") : "-")),
      res && typeof res === "object" && Object.keys(res).length
        ? h("div", { class: "card" }, h("h3", {}, "Agent 回传结果"),
            h("pre", { class: "json-block", style: "white-space:pre-wrap;max-height:300px" },
              JSON.stringify(res, null, 2)))
        : h("div", { class: "note" }, "任务尚未回传结果——Agent 执行完成后这里会有真实输出。"));
  }

  async function confirmRun(r, btn) {
    try {
      const res = await busy(btn, "确认中…", () => api.post(`/api/ops/heal-policy-runs/${r.id}/confirm`));
      toast(`已下发（任务 #${res.task_id}），Agent 执行后会做后置校验`, "ok");
      await draw();
    } catch (e) { toast(`确认失败：${e.message}`, "error"); }
  }

  async function dryRun(r, btn) {
    try {
      const res = await busy(btn, "演练中…", () => api.post(`/api/ops/heal-policies/${r.id}/dry-run`));
      const v = res.verdict || {};
      drawer(`护栏演练 · ${r.name}`,
        note(`演练目标：**${res.service}**（动作：${res.action_label}）。护栏结论：`
          + (v.allow ? "**允许**——真触发时会按当前执行档下发" : `**拒绝**（${v.rule}）：${v.reason}`)),
        h("div", { class: "detail-meta" },
          kv("允许", v.allow ? badge("是", "PASS") : badge("否", "FAIL")),
          kv("护栏规则", h("span", { class: "mono" }, v.rule || "-")),
          ...(v.warnings || []).map((w) => kv("提醒", h("span", { class: "muted" }, w)))),
        h("div", { class: "note" },
          "演练不下发任何动作。想真跑：触发条件命中后按执行档执行，或在执行日志里确认。"));
    } catch (e) { toast(`演练失败：${e.message}`, "error"); }
  }

  async function toggle(r, btn) {
    try {
      await busy(btn, r.enabled ? "停用中…" : "启用中…",
        () => api.put(`/api/ops/heal-policies/${r.id}`, { ...r, enabled: !r.enabled }));
      toast(r.enabled ? "已停用（不再触发）" : "已启用", "ok");
      draw();
    } catch (e) { toast(`操作失败：${e.message}`, "error"); }
  }

  async function del(r, btn) {
    if (!window.confirm(`删除自愈策略「${r.name}」？执行日志会保留（可审计）。`)) return;
    try { await busy(btn, "删除中…", () => api.del(`/api/ops/heal-policies/${r.id}`)); toast("已删除", "ok"); draw(); }
    catch (e) { toast(`删除失败：${e.message}`, "error"); }
  }

  // ── 新建/编辑抽屉 ──
  async function editor(p) {
    let rules = [], services = [];
    try { rules = (await api.get("/api/ops/alert-rules", { page_size: 200 })).items || []; } catch { /* ignore */ }
    try { services = (await api.get("/api/ops/services")).items || []; } catch { /* ignore */ }

    const name = h("input", { class: "vs-input", value: p?.name || "", placeholder: "例：连接数满自动重连" });
    const trigType = select([["rule", "关联告警规则（推荐）"], ["metric", "直填指标"]], p?.trigger_rule_id ? "rule" : "metric");
    const ruleSel = select([["", "选择告警规则…"], ...rules.map((r) => [String(r.id), `${r.name}（S${r.severity}）`])],
      String(p?.trigger_rule_id || ""));
    const metricIn = h("input", { class: "vs-input mono", list: "heal-metric-list", value: p?.trigger_metric || "",
      placeholder: "cpu.percent" });
    // D142 AI 阈值推荐：结果**内嵌在编辑抽屉里**（此前用第二层 drawer 会把编辑抽屉顶掉，
    // 「采用此阈值」点完主抽屉已消失、阈值填不进去——实测交互 bug）
    const recBox = h("div", { class: "rec-box" });
    const recBtn = h("button", { class: "btn ghost", type: "button",
      title: "基于历史数据推荐触发阈值（附依据）",
      onclick: async (e) => {
        const metric = metricIn.value.trim();
        if (!metric) { toast("先填指标名，再点推荐", "warn"); return; }
        try {
          const res = await busy(e.currentTarget, "分析中…", () =>
            api.post("/api/ops/heal-policies/recommend-threshold", { metric, op: opSel.value, days: 7 }));
          if (!res.ok) { toast(res.reason, "warn"); return; }
          thIn.value = res.recommended;
          recBox.replaceChildren(
            h("div", { class: "rec-title" }, `AI 推荐（近 ${res.days} 天 · ${res.samples} 个数据点）`),
            h("div", { class: "rec-rationale" }, res.rationale + (res.trend ? "　" + res.trend : "")),
            h("div", { class: "rec-meta mono" },
              `中位数 ${res.p50} · p90 ${res.p90} · p95 ${res.p95} · p99 ${res.p99} · 峰值 ${res.peak}`),
            h("div", { class: "rec-actions" },
              h("button", { class: "btn secondary", type: "button",
                onclick: () => { thIn.value = res.recommended; toast(`已填入推荐阈值 ${res.recommended}`, "ok"); } },
                "采用此阈值")));
        } catch (err) { toast(`推荐失败：${err.message}`, "error"); }
      } }, svgIcon("sparkles", 12), h("span", {}, "AI 推荐阈值"));
    const metricList = h("datalist", { id: "heal-metric-list" },
      ...METRIC_PRESETS.map(([v, l]) => h("option", { value: v }, l)));
    const opSel = select([[">=", "≥ 超过"], ["<=", "≤ 低于"]], p?.trigger_op || ">=");
    const thIn = h("input", { class: "vs-input mono", type: "number", value: p?.trigger_threshold ?? 90 });
    const sevSel = select([["1", "S1 紧急"], ["2", "S2 警告"], ["3", "S3 通知"]], String(p?.trigger_severity ?? 1));
    const actionSel = select([
      ["restart_service", "重启服务（systemd）"],
      ["restart_container", "重启容器（Docker）"],
      ["reconnect_pool", "重连连接池（DB 连接满首选）"],
      ["rotate_logs", "轮转日志（磁盘满首选）"],
    ], p?.action || "reconnect_pool");
    const svcSel = select([["", "自动解析（告警主机上的服务）"],
      ...services.map((s0) => [String(s0.id), `${s0.name} · ${s0.host || ""}`])],
      String(p?.service_id || ""));
    const modeSel = select([
      ["suggest", "仅建议（只评估留痕，不下发）"],
      ["manual", "人工确认（护栏通过后等你点确认）"],
      ["auto", "自动执行（护栏通过即下发——生产慎用）"],
    ], p?.mode || "suggest");
    const cdIn = h("input", { class: "vs-input mono", type: "number", value: p?.cooldown_min ?? 15 });
    const predictIn = h("input", { class: "vs-input mono", type: "number", value: p?.predict_min ?? 0,
      min: "0", max: "240" });

    const syncTrig = () => {
      const isRule = trigType.value === "rule";
      ruleSel.parentElement.style.display = isRule ? "" : "none";
      metricIn.parentElement.style.display = isRule ? "none" : "";
      opSel.parentElement.style.display = isRule ? "none" : "";
      thIn.parentElement.style.display = isRule ? "none" : "";
    };
    trigType.addEventListener("change", syncTrig);

    drawer(p ? `编辑自愈策略 · ${p.name}` : "新建自愈策略",
      note("动作与护栏和人工恢复**完全同源**——白名单外的动作进不来，护栏不过不下发。"
        + "推荐先用「仅建议」档上线，看几天执行日志确认护栏判断符合预期，再升「人工确认」或「自动执行」。"),
      frow(reqLabel("策略名"), name),
      h("div", { class: "form-row" }, h("label", {}, reqLabel("触发方式")), trigType),
      h("div", { class: "form-row" }, h("label", {}, reqLabel("告警规则"),
        helpTip("策略挂在规则上：该规则触发告警时本策略随之评估。规则的防抖（持续时长）、静默、分级通知都自动生效。")), ruleSel),
      h("div", { class: "form-row" }, h("label", {}, "指标名",
        helpTip("直填指标时与告警引擎独立比对。常用：" + METRIC_PRESETS.map(([v, l]) => `${v}（${l}）`).join("、"))),
        h("div", { style: "display:flex;gap:8px;align-items:center;width:100%" }, metricIn, recBtn)),
      recBox,
      metricList,
      h("div", { class: "form-row" }, h("label", {}, "比较"), opSel),
      h("div", { class: "form-row" }, h("label", {}, reqLabel("阈值",
      ), helpTip("建议先用「仅建议」档跑几天，从执行日志里看真实触发值，再收紧阈值。")), thIn),
      h("div", { class: "form-row" }, h("label", {}, "事件级别",
        helpTip("用于留痕归类与展示；真正的门槛是阈值本身。")), sevSel),
      h("div", { class: "form-row" }, h("label", {}, reqLabel("动作",
      ), helpTip("白名单动作。重启服务=systemd 重启；重连连接池=应用重启的语义化动作（DB 连接满首选）；轮转日志=磁盘满时清日志的缓解动作。")), actionSel),
      h("div", { class: "form-row" }, h("label", {}, "目标服务",
        helpTip("动作作用于服务声明里的 systemd 单元/容器名。留空=自动取告警主机上的第一个已登记服务；也可在这里指定。")), svcSel),
      h("div", { class: "form-row" }, h("label", {}, ["执行档", helpTip("生产环境建议「人工确认」起步。自动执行要求护栏全通过，且每次都会留痕与审计。")]), modeSel),
      h("div", { class: "form-row" }, h("label", {}, ["冷却(分钟)", helpTip("同一策略两次触发的最小间隔——防止「自愈 → 又告警 → 又自愈」循环。默认 15 分钟。")]), cdIn),
      // D146：这里**必须用 predictIn**——此前表单行又 new 了一个同名语义的 input，
      // 保存时读的却是上面那个 predictIn（初值 0）→ 用户在界面填的「预测提前量」
      // 根本没保存（predict_min 恒为 0，D143 预测预警在界面上配不出来）。
      h("div", { class: "form-row" }, h("label", {}, ["预测预警(分钟，0=关)", helpTip("趋势外推：按最近 30 分钟的指标走向，提前 N 分钟预测是否越过阈值。"
        + "预测命中只**通知**（全渠道），不自动执行——预测有误报，是否动手由人判断。仅「直填指标」模式支持。")]),
        predictIn ),
      h("div", { class: "detail-actions" },
        h("button", { class: "btn", onclick: async (e) => {
          const bad = needText(name.value, "策略名", 128)
            || (trigType.value === "rule" && !ruleSel.value ? "请选择触发用的告警规则" : "")
            || (trigType.value === "metric" && !metricIn.value.trim() ? "直填指标模式必须填指标名" : "");
          if (bad) { toast(bad, "warn"); return; }
          const body = {
            name: name.value.trim(), enabled: p ? p.enabled : true,
            trigger_rule_id: trigType.value === "rule" ? Number(ruleSel.value) || null : null,
            trigger_metric: trigType.value === "metric" ? metricIn.value.trim() : "",
            trigger_op: opSel.value, trigger_threshold: Number(thIn.value) || 90,
            trigger_severity: Number(sevSel.value),
            action: actionSel.value,
            service_id: svcSel.value ? Number(svcSel.value) : null,
            mode: modeSel.value, cooldown_min: Number(cdIn.value) || 15,
            predict_min: Number(predictIn.value) || 0,
          };
          try {
            await busy(e.currentTarget, "保存中…", () => p
              ? api.put(`/api/ops/heal-policies/${p.id}`, body)
              : api.post("/api/ops/heal-policies", body));
            toast("已保存——先用「演练」看护栏结论，再决定升档", "ok");
            document.querySelector(".detail-head .btn")?.click();
            draw();
          } catch (err) { toast(`保存失败：${err.message}`, "error"); }
        } }, p ? "保存" : "创建")));

    syncTrig();
  }

  function frow(label, el) { return h("div", { class: "form-row" }, h("label", {}, label), el); }
  function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v)); }

  await draw();
}

export function destroy() {}
