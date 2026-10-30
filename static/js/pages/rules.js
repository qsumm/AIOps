// pages/rules.js —— 告警规则（FR-F ops-rules）
// 必备：规则表格 + 编辑抽屉（作用域/表达式/时段/通知参数/异常检测开关）、测试运行按钮
import { api, getUser, can } from "../api.js";
import { fill, h, table, sevBadge, drawer, toast, select, badge, note, busy, reqLabel, needText, needUrl, statusBadge, helpTip, pager } from "../ui.js";

const ALGOS = [["threshold", "阈值"], ["3sigma", "3σ 异常检测"], ["isolation_forest", "孤立森林"]];
const SCOPES = [["metric", "指标（Agent 上报）"], ["probe", "探活（Agent 上报）"],
  ["service", "服务状态（服务监控）"], ["prom", "外部指标源"]];

export async function render(root) {
  const me = getUser() || {};
  const canWrite = can("write");       // 规则 CRUD：admin 专属
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));
  const state = { show: "enabled" , page: 1, pageSize: 50 };   // D61：默认只看启用中的规则（停用的历史行不再永久刷屏）

  const head = h("div", { class: "page-head" },
    h("div", { class: "actions" },
      select([["enabled", "启用中"], ["disabled", "已停用"], ["all", "全部"]], state.show,
        { onchange: (e) => { state.show = e.target.value; state.page = 1; draw(); } }),
      h("button", { class: "btn secondary", onclick: () => ds_dialog() }, "外部指标源"),
      h("button", { class: "btn secondary", onclick: () => metric_query_dialog() }, "指标查询"),
      canWrite ? h("button", { class: "btn", onclick: () => editor(null) }, "新建规则") : null));
  const body = h("div", { id: "rules-body" });
  const pagerEl = h("div", { class: "pager" });   // D128
  root.replaceChildren(head, body, pagerEl)

  async function draw() {
    let d;
    // D174：筛选交给服务端（此前在前端当前页数据上过滤 → 跨页漏规则、计数也不对）
    try { d = await api.get("/api/ops/alert-rules", { page: state.page, page_size: state.pageSize, show: state.show }); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    const items = d.items || [];
    // D62：计数行（页面自己会说话；顺带告知「已停用」去哪了）——D174 起计数取服务端全量
    const c = d.counts || { all: items.length, enabled: items.filter((r) => !r.disabled).length, disabled: items.filter((r) => r.disabled).length };
    body.replaceChildren(
      h("div", { class: "count-line muted" },
        c.all
          ? `共 ${c.all} 条规则 · 启用 ${c.enabled} · 已停用 ${c.disabled}（切右上筛选查看）`
          : ""),
      table([
      { title: "序号", width: "46px", render: (_r, i) => h("span", { class: "mono muted" }, String(i + 1)) },   // D129
      { title: "名称", render: (r) => h("span", {}, r.name) },
      { title: "目标", width: "150px", render: (r) => h("span", { class: "mono" }, r.scope === "prom" ? (r.queries_json || []).join(" | ") : r.metric) },
      { title: "条件", width: "92px", render: (r) => h("span", { class: "mono" }, r.expr) },
      { title: "级别", width: "88px", render: (r) => sevBadge(r.severity) },
      { title: "操作", width: "216px", render: (r) => h("div", { class: "row-actions" },
          h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); test(r, e.currentTarget); } }, "测试运行"),
          canWrite ? h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); editor(r); } }, "编辑") : null,
          // D61（用户报）：停用后没有任何办法重新启用——现在双向开关，行内直接切
          canWrite ? h("button", { class: "btn ghost", onclick: async (e) => {
            try { await busy(e.currentTarget, r.disabled ? "启用中…" : "停用中…", () => api.post(`/api/ops/alert-rules/${r.id}/toggle`));
              toast(r.disabled ? "已启用，下一轮评估开始生效" : "已停用（切到「已停用」筛选可重新启用）", "ok"); draw(); }
            catch (err) { toast(`失败：${err.message}`, "error"); }
          } }, r.disabled ? "启用" : "停用") : null,
          // D61：停用规则可真删（默认软删只是停用）——不然废弃规则永远清不掉
          canWrite && r.disabled ? h("button", { class: "btn ghost danger", onclick: async (e) => {
            e.stopPropagation();
            if (!window.confirm(`彻底删除规则「${r.name}」？此操作不可恢复（历史告警仍保留）。`)) return;
            try { await busy(e.currentTarget, "删除中…", () => api.del(`/api/ops/alert-rules/${r.id}?purge=true`));
              toast("已删除", "ok"); draw(); }
            catch (err) { toast(`失败：${err.message}`, "error"); }
          } }, "删除") : null) },
    // D22 同类修复：新建按钮对 demo 隐藏，空态提示不能指向它
    ], items, { emptyText: state.show === "disabled" ? "没有已停用的规则"
      : canWrite ? "暂无规则——点右上「新建规则」" : "暂无规则（新建规则需要 admin 权限）" }));
    pagerEl.replaceChildren(pager({ total: d.total || 0, page: state.page, pageSize: state.pageSize, unit: "条",
      onChange: (p, ps) => { state.page = p; if (ps !== state.pageSize) state.pageSize = ps; draw(); } }));
    pagerEl.replaceChildren(pager({ total: d.total || 0, page: state.page, pageSize: state.pageSize,
      onChange: (p, ps) => { state.page = p; if (ps !== state.pageSize) state.pageSize = ps; draw(); } }));
  }

  const scopeLabel = (s) => (SCOPES.find((x) => x[0] === s) || [, s])[1];

  async function test(r, btn) {
    try {
      const d = await busy(btn, "试运行中…", () => api.post(`/api/ops/alert-rules/${r.id}/test`));
      if (d.error) { toast(`试运行：${d.error}`, "warn"); return; }
      drawer(`试运行 · ${r.name}`,
        h("div", { class: "note" }, `命中 ${d.anomaly_count} 个异常点（未落事件）· 生效算法 ${d.algorithm_effective}`),
        table([
          { title: "主机", render: (x) => h("span", { class: "mono" }, x.host) },
          { title: "指标", render: (x) => h("span", { class: "mono" }, x.metric) },
          { title: "值", width: "110px", render: (x) => h("span", { class: "mono" }, x.value) },
        ], d.matched || [], { emptyText: "当前窗口内无命中（正常）" }));
    } catch (e) { toast(`试运行失败：${e.message}`, "error"); }
  }

  async function editor(rule) {
    let dss = [], chans = [];
    try { dss = (await api.get("/api/ops/prom-datasources")).items || []; } catch { /* ignore */ }
    try { chans = (await api.get("/api/ops/notify-channels")).items || []; } catch { /* ignore */ }
    // D149：阈值提醒容器（百分比指标 + 明显偏离常识的阈值时显示；只提示不拦截）
    const exprWarn = h("div", { class: "note", hidden: "" });
    const f = {
      name: h("input", { class: "vs-input", value: rule?.name || "" }),
      scope: select(SCOPES, rule?.scope || "metric"),
      // D160：默认值必须随作用域——探活/服务域的"目标名"是**探活名/服务名**（不是指标名），
      // 以前一律默认填 cpu.percent → 引擎去匹配不存在的探活名 → 规则永不触发。
      metric: h("input", { class: "vs-input mono", value: rule?.metric || "",
        placeholder: "指标名（探活/服务类留空=全部）" }),
      queries: h("input", { class: "vs-input mono", value: (rule?.queries_json || []).join(",") }),
      datasource: select([["", "（默认数据源）"], ...dss.map((d) => [String(d.id), d.name])], rule?.datasource_id ?? ""),
      expr: h("input", { class: "vs-input mono", value: rule?.expr || ">90" }),
      severity: select([["1", "S1 紧急"], ["2", "S2 警告"], ["3", "S3 通知"]], String(rule?.severity ?? 2)),
      algorithm: select(ALGOS, rule?.algorithm || "threshold"),
      window_s: h("input", { class: "vs-input mono", type: "number", value: rule?.window_s ?? 300 }),
      for_duration_s: h("input", { class: "vs-input mono", type: "number", value: rule?.for_duration_s ?? 0 }),
      recover_duration_s: h("input", { class: "vs-input mono", type: "number", value: rule?.recover_duration_s ?? 300 }),
      eval_interval_s: h("input", { class: "vs-input mono", type: "number", value: rule?.eval_interval_s ?? 15 }),
      cron_pattern: h("input", { class: "vs-input mono", placeholder: "留空=固定间隔；如 */5 * * * *", value: rule?.cron_pattern || "" }),
      step: h("input", { class: "vs-input mono", type: "number", value: rule?.notify_repeat_step_min ?? 0 }),
      maxn: h("input", { class: "vs-input mono", type: "number", value: rule?.notify_max_number ?? 0 }),
      recovered: select([["1", "开启"], ["0", "关闭"]], String(rule ? (rule.notify_recovered ? 1 : 0) : 1)),
      channels: h("input", { class: "vs-input mono", value: (rule?.channels_json || []).join(","), placeholder: "留空=全部已启用渠道（分级配置优先于这里）" }),
    };
    // D160：目标名默认值随作用域——指标域给 cpu.percent 便捷默认；探活/服务域必须**留空**
    //（那里的"目标名"是探活名/服务名，填指标名会让规则静默零命中、永不触发）。
    const syncTargetDefault = () => {
      const sc = f.scope.value;
      if (sc === "metric") { if (!f.metric.value.trim()) f.metric.value = "cpu.percent"; }
      else if (f.metric.value.trim() === "cpu.percent") { f.metric.value = ""; }
    };
    f.scope.addEventListener("change", syncTargetDefault);
    syncTargetDefault();
    // D140 分级通知（用户原话：「严重的一定是电话通知，其他的无所谓」）：
    // S1 紧急 → 必须电话；S2/S3 → IM。每级一个多选（勾选渠道名），保存进 channels_by_level。
    const cbl = (rule?.channels_by_level && typeof rule.channels_by_level === "object") ? rule.channels_by_level : {};
    const levelSel = {};
    [["1", "S1 紧急 — 电话必选", true], ["2", "S2 警告 — IM 渠道", false], ["3", "S3 通知 — IM 渠道", false]]
      .forEach(([lv, labelText, isS1]) => {
        const chosen = new Set(cbl[lv] || []);
        const box = h("div", { class: "lv-channels" },
          ...chans.filter((c) => c.enabled).map((c) => {
            const cb = h("input", { type: "checkbox", checked: chosen.has(c.name) ? "checked" : null,
              onchange: () => { cb.checked ? chosen.add(c.name) : chosen.delete(c.name); } });
            return h("label", { class: "lv-ch" },
              cb,
              h("span", { class: c.type === "phone" ? "lv-ch-name is-phone" : "lv-ch-name" },
                c.name,
                c.type === "phone" ? h("span", { class: "badge FAIL", style: "margin-left:6px" }, "电话") : null));
          }),
          chans.filter((c) => c.enabled).length
            ? null
            : h("div", { class: "muted", style: "font-size:0.76rem" },
                "还没有可用渠道——先到「系统设置 → 通知渠道」添加并启用"));
        levelSel[lv] = chosen;
        // 需要放进表单的 DOM：挂在分级卡里
        levelSel[lv + "_box"] = h("div", { class: "lv-row" },
          h("div", { class: "lv-row-label" }, labelText), box);
      });
    const collectLevels = () => {
      const out = {};
      for (const [lv, set] of Object.entries(levelSel)) {
        if (lv.endsWith("_box")) continue;
        const arr = [...set];
        if (arr.length) out[lv] = arr;
      }
      return Object.keys(out).length ? out : null;
    };
    /* D144：生效时段改成纯时间选择框（用户原话：「改为时间选择框，如 00:00~23:59，用户自己选」）。
       标题明确标注 UTC+8；后端按北京时间判定（engine/core.py 已换算）。
       只保留一个时间范围 —— 默认 00:00~23:59 = 全天。按星期/多时段的老规则走「高级」JSON。 */
    const segs0 = (rule?.effective_time || []).filter((s) => s && (s.start || s.end));
    const first = segs0[0] || {};
    const effStart = h("input", { class: "vs-input mono", type: "time", value: first.start || "00:00" });
    const effEnd = h("input", { class: "vs-input mono", type: "time", value: first.end || "23:59" });
    const weekdayOnly = segs0.some((s) => Array.isArray(s.days) && s.days.length && s.days.length < 7);
    const effNote = h("div", { class: "muted", style: "font-size:0.74rem" },
      weekdayOnly
        ? "该规则当前还带按星期限制（原设置）。用上面的时间框保存后会改为「每天生效」；如需保留星期范围，请展开下方「高级」直接写 JSON。"
        : "只在这个时间段内评估与发送通知；00:00~23:59 表示全天。时间按北京时间（UTC+8）解释。");
    const collectEff = () => {
      const s = effStart.value || "00:00", e = effEnd.value || "23:59";
      if (s === "00:00" && e === "23:59") return [];          // 全天 = 空数组（后端既有语义）
      return [{ days: [1, 2, 3, 4, 5, 6, 7], start: s, end: e }];
    };
    // 高级：留空 = 用上面的时间框；填了才覆盖（旧实现把原值预填进这里，
    // 导致上方结构化编辑永远不生效——D144 一并修掉）
    const rawFallback = h("input", { class: "vs-input mono", value: "",
      placeholder: segs0.length ? `当前：${JSON.stringify(segs0)}（留空 = 用上面的时间框）` : '如 [{"days":[1,2,3,4,5],"start":"09:00","end":"18:00"}]' });
    // D149：百分比指标 + 明显不符合常识的阈值 → 编辑时提示（不拦截，用户仍可保存）
    const syncExprWarn = () => {
      const metric = (f.metric.value || "").trim();
      const ex = (f.expr.value || "").trim();
      const m = ex.match(/^([<>]=?)\s*(-?\d+(?:\.\d+)?)/);
      let msg = "";
      if (m && /\.percent$|\.pct$/i.test(metric)) {
        const op = m[1], th = Number(m[2]);
        if (op.startsWith(">") && th < 10) {
          msg = `「${metric} ${ex}」的阈值只有 ${th}%——磁盘/内存/CPU 通常按 80~90 设置，这样会一直接近满值误报。`;
        } else if (op.startsWith("<") && th > 90) {
          msg = `「${metric} ${ex}」要求低于 ${th}% 才告警——通常按 10~20 设置，否则几乎永不触发。`;
        }
      }
      exprWarn.hidden = !msg;
      exprWarn.replaceChildren(msg ? h("b", {}, "阈值提醒：") : "", msg);
    };
    f.metric.addEventListener("input", syncExprWarn);
    f.expr.addEventListener("input", syncExprWarn);
    syncExprWarn();
    drawer(rule ? `编辑规则 · ${rule.name}` : "新建告警规则",
      h("div", { class: "rule-editor" },
        h("h3", { class: "rule-editor-title" }, rule ? "编辑告警规则" : "新建告警规则"),
        h("div", { class: "rule-editor-sub" },
          rule ? "修改后保存立即生效（下一轮评估起使用新配置）。"
               : "分四段填写：基本信息、触发条件、通知设置、生效时间。带 * 的为必填。"),

        h("section", { class: "form-section" },
          h("h3", { class: "form-section-title" }, "基本信息", h("small", {}, "告警看什么")),
          h("div", { class: "grid-3" },
            frow(reqLabel("名称"), f.name),
            frow(["作用域", helpTip("告警的数据来源：**指标**=Agent 上报的时序（cpu / 内存 / 磁盘 / 负载）；**探活**=主机上配置的探活项（端口 / HTTP / 进程 / 证书）；**外部指标源**=向公司已有的外部监控系统查询现成指标。")], f.scope),
            frow([reqLabel("目标名（指标 / 探活 / 服务）"), helpTip("按作用域填不同东西：**指标**=cpu.percent / mem.percent 等（必填）；**探活**=探活项名称，**留空=任意探活失败**；**服务**=服务名，**留空=该主机全部服务**；**外部指标源** 不用这一项（填下面的查询语句）。"
              + "**指标支持通配符**：`disk.*.percent` 一条规则覆盖该主机所有分区（每块盘分别告警、分别恢复）；`*` 只匹配一段（不跨 `.`，所以不会误命中 `disk.inode.*`），要跨段用 `**`；也可写 `disk.data*.percent` 只匹配某个前缀。")], f.metric),
            frow(["指标查询语句（外部指标源，多条用逗号分隔）", helpTip("填向外部监控系统取数用的查询语句（业界最常用的是开源软件 Prometheus，它的查询语法叫 PromQL），多条用逗号分隔。")], f.queries),
            frow("使用哪个外部指标源", f.datasource))),

        h("section", { class: "form-section" },
          h("h3", { class: "form-section-title" }, "触发条件", h("small", {}, "什么时候算异常")),
          h("div", { class: "grid-3" },
            frow([reqLabel("条件表达式"), helpTip("与指标值比较的表达式：如 >85（超过 85 触发）、<30（低于 30，适合「证书剩余天数」这类反向指标）。")], f.expr),
            frow("级别", f.severity),
            frow(["检测算法", helpTip("阈值=超过表达式就给分；3σ=偏离近期均值 3 个标准差（适合没有固定阈值的指标）；孤立森林=机器学习离群检测（依赖 scikit-learn，环境不支持时自动降级为 3σ）。")], f.algorithm),
            frow(["评估窗口(s)", helpTip("每次评估取最近多少秒的数据来看（窗口越短越灵敏、越容易抖动）。")], f.window_s),
            frow(["持续时长(s)", helpTip("异常需要**连续持续**这么久才真正触发告警（防抖）；0 表示一命中就触发。")], f.for_duration_s),
            frow(["恢复留观(s)", helpTip("恢复正常后还要再稳定这么久才判定恢复并发恢复通知——避免指标抖动导致「告警/恢复」来回刷。")], f.recover_duration_s),
            frow(["评估间隔(s)", helpTip("多久评估一次；与下面的 cron 二选一。")], f.eval_interval_s),
            frow(["或 cron 表达式", helpTip("按点评估（如每天 09:00 查一次证书到期）。注意用 **UTC** 时区，避免服务器时区不同跑错点。")], f.cron_pattern)),
          exprWarn),

        h("section", { class: "form-section" },
          h("h3", { class: "form-section-title" }, "通知设置", h("small", {}, "通知谁、通知几次")),
          h("div", { class: "grid-3" },
            frow(["重发步长(min，0=只发一次)", helpTip("告警持续期间重复通知的间隔；0=只在触发时发一次（告警风暴期首选）。")], f.step),
            frow(["封顶条数(0=不限)", helpTip("同一条告警最多通知几次，超出后静默——长期故障不会一直刷屏（状态仍在告警中心可见）。")], f.maxn),
            frow("恢复通知", f.recovered),
            frow(["兜底渠道（分级未覆盖时用）", helpTip("分级配置里没覆盖到的级别，按这里填的渠道发；留空=全部已启用渠道。")], f.channels)),
          // D140 分级通知卡（放在渠道行之后，视觉上与通知相关项聚在一起）
          h("div", { class: "form-row" },
            h("label", {}, "分级通知（推荐）",
              helpTip("按告警级别分别指定渠道：**S1 紧急必须走电话**（保存时会强校验）；S2 警告 / S3 通知走企微、钉钉、飞书等 IM。"
                + "分级配置优先于上面的兜底渠道；某一级不勾任何渠道 = 该级回落到兜底渠道。")),
            h("div", { class: "lv-card" },
              levelSel["1_box"], levelSel["2_box"], levelSel["3_box"]))),

        h("section", { class: "form-section" },
          h("h3", { class: "form-section-title" }, "告警通知生效时间 (UTC+8)",
            h("small", {}, "只在这个时间段内评估与发送通知")),
          h("div", { class: "eff-time-row" },
            effStart, h("span", { class: "separator" }, "~"), effEnd),
          effNote,
          h("details", { class: "rule-advanced" },
            h("summary", {}, "高级：按星期 / 多时段（JSON）"),
            h("div", {}, rawFallback))),

        h("div", { class: "detail-actions" },
          h("button", { class: "btn", onclick: async (e) => {
          // D59：名称必填；指标类必须有指标名；Prometheus 类必须至少一条 PromQL
          // D60：生效时段取 raw JSON 兜底；raw 留空/[] 按结构化编辑器；都为 [] = 全天生效
          const queries = f.queries.value.split(",").map((x) => x.trim()).filter(Boolean);
          const effRaw = (rawFallback.value || "").trim();
          let effVal = null;
          if (effRaw && effRaw !== "[]") effVal = safeJson(effRaw, null);
          if (effVal === null) effVal = collectEff();
          if (effVal === null || (Array.isArray(effVal) && !effVal.length)) effVal = [];
          if (!Array.isArray(effVal)) { toast("生效时段 JSON 必须是数组", "error"); return; }
          const bad = needText(f.name.value, "规则名称", 128)
            // 只有「指标」作用域必须填目标名：引擎明确支持「探活留空=任意探活失败」「服务留空=全部服务」
            || (f.scope.value === "metric" ? needText(f.metric.value, "指标名", 64) : "")
            || (f.scope.value === "prom" && !queries.length ? "外部指标源规则至少填写一条查询语句" : "")
            || needText(f.expr.value, "条件表达式");
          if (bad) { toast(bad, "error"); return; }
          const body = {
            name: f.name.value.trim(), scope: f.scope.value, metric: f.metric.value.trim(),
            queries_json: queries,
            datasource_id: f.datasource.value ? Number(f.datasource.value) : null,
            expr: f.expr.value, severity: Number(f.severity.value), algorithm: f.algorithm.value,
            window_s: Number(f.window_s.value), for_duration_s: Number(f.for_duration_s.value),
            recover_duration_s: Number(f.recover_duration_s.value), eval_interval_s: Number(f.eval_interval_s.value),
            cron_pattern: (f.cron_pattern.value || "").trim(),
            notify_repeat_step_min: Number(f.step.value), notify_max_number: Number(f.maxn.value),
            notify_recovered: f.recovered.value === "1",
            channels_json: f.channels.value.split(",").map((x) => x.trim()).filter(Boolean),
            channels_by_level: collectLevels(),
            effective_time: effVal,
          };
          try {
            // D51：保存要校验表达式 + 落库，超时也要给中间态
            await busy(e.currentTarget, "保存中…", () => rule
              ? api.put(`/api/ops/alert-rules/${rule.id}`, body)
              : api.post("/api/ops/alert-rules", body));
            toast("已保存", "ok"); document.querySelector(".detail-head .btn")?.click(); draw();
          } catch (err) { toast(`保存失败：${err.message}`, "error"); }
        } }, rule ? "保存修改" : "创建规则"))));
  }

  async function ds_dialog(ds) {
    // D59：支持编辑（后端 PUT 原先就有，UI 只有新建——改个地址只能删了重建）。
    // 密码留空 = 不修改（服务端既有语义）。
    const editing = !!ds;
    let d = { items: [] };
    try { d = await api.get("/api/ops/prom-datasources"); } catch { /* ignore */ }
    const name = h("input", { class: "vs-input", placeholder: "prod-prom", value: ds?.name || "" });
    const url = h("input", { class: "vs-input mono", placeholder: "http://127.0.0.1:9090", value: ds?.base_url || "" });
    const auth = select([["none", "无认证"], ["basic", "Basic"], ["bearer", "Bearer Token"]], ds?.auth_type || "none");
    const user = h("input", { class: "vs-input mono", placeholder: "用户名（Basic）", value: ds?.username || "" });
    const secret = h("input", { class: "vs-input", type: "password", placeholder: ds?.has_secret ? "留空 = 不修改已存凭据" : "密码 / Token" });
    // 两种模式——查询代理（走 Prometheus API）/ 直采 exporter（零改造接入 node_exporter）
    const mode = select([["query", "查询代理（实时查询外部系统）"], ["scrape", "定时拉取（直连指标接口）"]], ds?.mode || "query")
    // D157：定时拉取的间隔（仅 mode=scrape 显示）。常见档位 + 0=不自动拉（仅手动试采）
    const savedInterval = parseInt(ds?.scrape_interval_s, 10);
    const initialInterval = Number.isFinite(savedInterval) ? savedInterval : 30;
    const intervalOpts = [
      ["15", "15 秒（高频）"], ["30", "30 秒（默认）"], ["60", "1 分钟"],
      ["120", "2 分钟"], ["300", "5 分钟"], ["600", "10 分钟"],
      ["0", "0 = 不自动拉（仅手动试采）"]
    ];
    const interval = select(intervalOpts, String(initialInterval));
    const intervalRow = h("div", { class: "form-row" }, h("label", {}, "拉取间隔"), interval);
    const updateIntervalVisibility = () => { intervalRow.style.display = mode.value === "scrape" ? "" : "none"; };
    mode.addEventListener("change", updateIntervalVisibility);
    const mpath = h("input", { class: "vs-input mono", placeholder: "/metrics", value: ds?.scrape_config?.path || "/metrics" });
    const mkeep = h("input", { class: "vs-input mono", placeholder: "额外保留的指标名正则，逗号分隔（可留空）",
      value: (ds?.scrape_config?.metric_keep || []).join(",") });
    const dsPanel = drawer("外部指标源", {width: 1040},
      note("**查询代理**：平台不存数据，告警判断时实时向外部监控系统查询。　"
        + "**定时拉取**：每 30 秒自动从目标机器的指标接口（/metrics）拉一次数据，转成平台指标（cpu.percent / mem.percent / "
        + "disk.percent / load.1）——机器上已装采集端（node_exporter）即可**零改造接入**，"
        + "会以「[scrape] 数据源名」出现在主机列表（注意：cpu.percent 是累计占比近似值，与 Agent 上报的瞬时值同量级）。"),
      table([
        { title: "名称", render: (r) => r.name },
        { title: "地址", render: (r) => h("span", { class: "mono" }, r.base_url) },
        { title: "模式", width: "76px", render: (r) => badge((r.mode || "query") === "scrape" ? "直采" : "代理", (r.mode || "query") === "scrape" ? "ACCENT" : "SKIP") },
        { title: "认证", width: "86px", render: (r) => h("span", { class: "muted" }, r.auth_type) },
        { title: "默认", width: "66px", render: (r) => r.is_default ? badge("是", "PASS") : "" },
        { title: "连通", width: "86px", render: (r) => statusBadge(r.last_check_status) },
        { title: "操作", width: "235px", render: (r) => h("div", { class: "row-actions" },
            h("button", { class: "btn ghost", onclick: async (e) => {
              // D51：要真连一次 Prometheus（网络）→ 中间态 + 已用秒数
              try { const t = await busy(e.currentTarget, "测试中…", () => api.post(`/api/ops/prom-datasources/${r.id}/test`));
                toast(t.ok ? `连通：${t.detail}` : `不通：${t.detail}`, t.ok ? "ok" : "error"); } catch (err) { toast(`测试失败：${err.message}`, "error"); }
              ds_dialog(); } }, "测试"),
            h("button", { class: "btn ghost", onclick: () => ds_dialog(r) }, "编辑"),
            (r.mode || "query") === "scrape" ? h("button", { class: "btn ghost", onclick: async (e) => {
              try { const t = await busy(e.currentTarget, "抓取中…", () => api.post(`/api/ops/prom-datasources/${r.id}/scrape-now`));
                toast(t.ok ? `直采成功：${Object.keys(t.metrics || {}).length} 项指标` : `直采失败：${t.note}`, t.ok ? "ok" : "error"); }
              catch (err) { toast(`直采失败：${err.message}`, "error"); } } }, "立即抓取") : null,
            h("button", { class: "btn ghost danger", onclick: async (e) => {
              try { await busy(e.currentTarget, "删除中…", () => api.del(`/api/ops/prom-datasources/${r.id}`)); }
              catch (err) { toast(`删除失败：${err.message}`, "error"); }
              ds_dialog(); } }, "删除")) },
      ], d.items, { emptyText: "尚未配置外部指标源（「外部指标源」类告警需要先在上方添加）" }),
      h("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:10px 16px" },
        frow(reqLabel("名称"), name), frow(reqLabel("地址"), url),
        frow("模式", mode), frow("认证", auth),
        frow("用户名", user), frow("密码/Token", secret),
        frow("直采路径", mpath), frow("额外指标正则", mkeep)),
      intervalRow,
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        // D59 客户端校验（服务端 _validate_ds 同口径）
        const bad = needText(name.value, "数据源名称", 64) || needText(url.value, "地址") || needUrl(url.value, "地址");
        if (bad) { toast(bad, "error"); return; }
        try {
          await busy(e.currentTarget, "保存中…", () => editing
            ? api.put(`/api/ops/prom-datasources/${ds.id}`, { name: name.value.trim(), base_url: url.value.trim(), auth_type: auth.value,
                username: user.value, secret: secret.value, enabled: ds.enabled, is_default: ds.is_default, mode: mode.value,
                scrape_config: { path: mpath.value || "/metrics",
                                 metric_keep: (mkeep.value || "").split(",").map((x) => x.trim()).filter(Boolean) },
                ...(mode.value === "scrape" ? {scrape_interval_s: Number(interval.value)} : {}) })
            : api.post("/api/ops/prom-datasources", { name: name.value.trim(), base_url: url.value.trim(), auth_type: auth.value,
                username: user.value, secret: secret.value, is_default: !d.items.length, mode: mode.value,
                scrape_config: { path: mpath.value || "/metrics",
                                 metric_keep: (mkeep.value || "").split(",").map((x) => x.trim()).filter(Boolean) },
                ...(mode.value === "scrape" ? {scrape_interval_s: Number(interval.value)} : {}) }));
          toast(editing ? "已保存" : "已添加", "ok"); ds_dialog();
        } catch (err) { toast(`失败：${err.message}`, "error"); }
      } }, editing ? "保存修改" : "添加数据源")));
    // D157：drawer() 同步 append DOM —— 立即执行显隐（query 模式隐藏间隔行）
    updateIntervalVisibility();
  }

  function frow(label, el) { return h("div", { class: "form-row" }, h("label", {}, label), el); }
  function safeJson(s, fallback) { try { return JSON.parse(s || "null") ?? fallback; } catch { return fallback; } }

  await draw();
}

export function destroy() {}


/* D154（FR-424）：指标即时查询——按指标（可带 {k=v} 维度）查窗口内各序列的聚合值。
   用途：告警说"这个指标超了"，用户要能自己下钻核对（信任闭环），而不是只能信 AI。 */
async function metric_query_dialog() {
  // 字段宽 100% 让 grid 等分；label 等长 → 上下对齐；聚合行独占整行 + 按钮右贴
  const metric = h("input", { class: "vs-input mono", value: "", placeholder: "如 pg.connections.current 或 pg.connections.current{server=db1}" });
  const host = h("input", { class: "vs-input mono", value: "", placeholder: "主机 ident（留空 = 全部）" });
  const tag = h("input", { class: "vs-input mono", value: "", placeholder: "维度筛选 k=v,k2=v2（可选）" });
  const agg = select([["avg", "平均"], ["max", "最大"], ["min", "最小"], ["p95", "P95"], ["p99", "P99"], ["sum", "求和"], ["count", "点数"]], "avg");
  const win = select([["15", "15 分钟"], ["60", "1 小时"], ["360", "6 小时"], ["1440", "24 小时"]], "60");
  const out = h("div", { class: "empty" }, "填好条件点「查询」");
  const draw = async (e) => {
    if (!metric.value.trim()) { toast("请填指标名", "error"); metric.focus(); return; }
    try {
      const btn = e?.currentTarget || null;
      const d = await busy(btn, "查询中…", () => api.get(
        `/api/ops/metric/query?metric=${encodeURIComponent(metric.value.trim())}`
        + `&host=${encodeURIComponent(host.value.trim())}&tag=${encodeURIComponent(tag.value.trim())}`
        + `&window=${win.value}&agg=${agg.value}`));
      const items = (d.series || []);
      if (!items.length) { out.replaceChildren(h("div", { class: "empty" }, "窗口内没有匹配的序列")); return; }
      fill(out,
        table([
          { title: "主机", render: (r) => r.ident },
          { title: "维度", render: (r) => Object.keys(r.tags || {}).length
              ? h("span", { class: "mono", style: "font-size:0.74rem" },
                  Object.entries(r.tags).map(([k, v]) => `${k}=${v}`).join(" ")) : "-" },
          { title: "点数", width: "70px", render: (r) => String(r.points) },
          { title: "末值", width: "110px", render: (r) => h("span", { class: "mono" }, String(r.last)) },
          { title: `${agg.toUpperCase()}`, width: "110px", render: (r) => h("span", { class: "mono" }, String(r.agg)) },
        ], items, { emptyText: "无序列" }),
        d.truncated ? h("div", { class: "note", style: "margin-top:8px" }, "结果被截断——请收窄条件（加主机/维度筛选）。") : null);
    } catch (err) { toast(`查询失败：${err.message}`, "error"); }
  };
  // 等长 label + 字段宽 100% 让 grid 等分；查询按钮独占整行右贴
  const lbl = (txt) => h("label", { style: "display:inline-block;min-width:64px" }, txt);
  drawer("指标查询（按维度下钻）",
    h("div", { class: "note" },
      "查的是平台自己收的指标（内置 Agent + exporter 直采，含 Categraf 中间件指标）。"
      + "聚合方式白名单：平均/最大/最小/P95/P99/求和/点数——不做任意表达式。"),
    h("div", { class: "form-row" }, lbl("指标名 *"), metric),
    h("div", { class: "form-row" }, lbl("主机"), h("input", { class: "vs-input mono", value: host.value, placeholder: host.placeholder, oninput: (e) => host.value = e.target.value })),
    h("div", { class: "form-row" }, lbl("维度"), h("input", { class: "vs-input mono", value: tag.value, placeholder: tag.placeholder, oninput: (e) => tag.value = e.target.value })),
    h("div", { class: "form-row" }, lbl("时间窗"), h("div", { style: "flex:1" }, win)),
    h("div", { class: "form-row" }, lbl("聚合方式"), h("div", { style: "flex:1" }, agg),
      h("button", { class: "btn", style: "margin-left:auto", onclick: draw }, "查询")),
    out);
}
