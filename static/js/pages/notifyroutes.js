// pages/notifyroutes.js —— 通知路由（FR-423，D154）
// 三个页签：通知规则（四维筛选 → 媒介 + 接收团队）/ 订阅（别的团队自己订）/ 业务组（主机归属 + 路由标签）
// 语义要点（与后端 services/notify_rules.py 同口径，写进界面提示防误配）：
//   · 适用级别**留空 = 一条都不匹配**（等于停用该条配置）——不是"不限制"
//   · 适用时段留空 = 不限制；支持跨零点；按北京时间判定
//   · 新规则命中即接管该事件（不再走规则上的「分级通知」）——互斥不是叠加
import { api, getUser, can } from "../api.js";
import { h, table, badge, drawer, toast, select, statusBadge, fill, busy, reqLabel, needText, helpTip, closeDrawer, note, svgIcon } from "../ui.js";

const SEV_OPTS = [["1", "S1 紧急"], ["2", "S2 警告"], ["3", "S3 通知"]];
const WDAYS = [["0", "一"], ["1", "二"], ["2", "三"], ["3", "四"], ["4", "五"], ["5", "六"], ["6", "日"]];
const OPS = [["==", "等于"], ["!=", "不等于"], ["=~", "正则匹配"], ["in", "属于列表"]];

export async function render(root) {
  const me = getUser() || {};
  const canWrite = can("write");
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  const tabs = h("div", { class: "tabs" });
  const body = h("div", { id: "nroute-body" });
  const HEAD = [["rules", "通知规则"], ["subs", "订阅"], ["groups", "业务组"]];
  let cur = "rules";

  async function draw() {
    if (cur === "rules") await drawRules();
    else if (cur === "subs") await drawSubs();
    else await drawGroups();
  }
  function setTab(k) {
    cur = k;
    tabs.replaceChildren(...HEAD.map(([k2, label]) =>
      h("button", { class: `tab${k2 === cur ? " active" : ""}`, onclick: () => setTab(k2) }, label)));
    draw();
  }

  // D169-b：页头两行结构——页签行（左页签、右主按钮）+ 说明行；
  // 此前页签/按钮/说明横挤一行，无层次无视觉锚点（用户报"毫无设计感"）。
  const newBtn = h("button", { class: "btn", id: "nroute-new", onclick: () => cur === "rules" ? ruleEditor() : cur === "subs" ? subEditor() : groupEditor() },
    cur === "rules" ? "新增规则" : cur === "subs" ? "新增订阅" : "新建业务组");
  function syncNewBtn() {
    newBtn.replaceChildren(cur === "rules" ? "新增规则" : cur === "subs" ? "新增订阅" : "新建业务组");
  }
  const _setTab = setTab;
  setTab = function (k) { _setTab(k); syncNewBtn(); };
  fill(root,
    h("div", { class: "page-head head-rows" },
      h("div", { class: "head-line" }, tabs,
        canWrite ? newBtn : null),
      h("div", { class: "head-sub" },
        svgIcon("info", 15),
        h("span", {}, "命中的事件由新规则接管，不再走告警规则上的「分级通知」；适用级别留空 = 该条配置停用。"),
        helpTip("语义要点：① 通知规则命中即接管，与「分级通知」互斥不是叠加；"
          + "② 适用级别留空 = 该条配置停用（不是\"不限制\"）；"
          + "③ 适用时段留空 = 不限制，按北京时间判定、支持跨零点；"
          + "④ 保存后用「试投」验证筛选条件（模拟事件，不真发）。"))),
    body);

  // ───────────────────────── 通知规则 ─────────────────────────
  async function drawRules() {
    let items = [], chans = [];
    try { items = (await api.get("/api/ops/notify-rules")).items || []; } catch (e) { body.replaceChildren(h("div", { class: "empty" }, e.message)); return; }
    try { chans = (await api.get("/api/ops/notify-channels")).items || []; } catch { /* 降级 */ }
    const chanName = (id) => (chans.find((c) => c.id === id) || {}).name || `渠道#${id}`;
    fill(body,
      table([
        { title: "名称", render: (r) => h("span", {}, r.name, r.description ? h("div", { class: "muted", style: "font-size:0.74rem" }, r.description) : null) },
        { title: "状态", width: "76px", render: (r) => badge(r.enabled ? "启用" : "停用", r.enabled ? "PASS" : "SKIP") },
        { title: "通知配置", render: (r) => h("div", { style: "font-size:0.78rem" },
            ...(r.configs || []).map((c, i) => h("div", {},
              `#${i + 1} ${chanName(c.channel_id)} · 级别 ${
                (c.severities || []).length ? (c.severities || []).map((s) => "S" + s).join("/") : "（停用）"}`)) ) },
        { title: "Pipeline", width: "90px", render: (r) => badge(String((r.pipelines || []).length) + " 个", (r.pipelines || []).length ? "ACCENT" : "SKIP") },
        { title: "操作", width: "150px", render: (r) => h("div", { class: "row-actions" },
            h("button", { class: "btn ghost", onclick: () => testRule(r) }, "试投"),
            canWrite ? h("button", { class: "btn ghost", onclick: () => ruleEditor(r) }, "编辑") : null,
            canWrite ? h("button", { class: "btn ghost", onclick: async (e) => {
              if (!window.confirm(`删除通知规则「${r.name}」？命中它的将回落到「分级通知」。`)) return;
              try { await busy(e.currentTarget, "删除中…", () => api.del(`/api/ops/notify-rules/${r.id}`)); toast("已删除", "ok"); }
              catch (err) { toast(`失败：${err.message}`, "error"); }
              draw(); } }, "删除") : null) },
      ], items, { emptyRender: () => h("div", { class: "empty", style: "padding:44px 0" },
          svgIcon("inbox", 40),
          h("h3", { style: "margin:12px 0 6px" }, "还没有通知规则"),
          h("p", { class: "muted", style: "max-width:460px; margin:0 auto 16px" },
            "事件目前仍走告警规则上的「分级通知」，行为不变。要按级别/标签/时段把告警发给不同团队时，新建一条即可。"),
          canWrite ? h("button", { class: "btn", onclick: () => ruleEditor() }, "新建通知规则") : null) }));
  }

  async function testRule(r) {
    try {
      const d = await busy(null, "求值中…", () => api.post(`/api/ops/notify-rules/${r.id}/test`));
      const rows = (d.configs || []).map((c, i) =>
        h("div", { style: "margin:6px 0" },
          `配置 #${i + 1}（渠道 ${c.channel_id}）：`,
          badge(c.matched ? "命中" : "不命中", c.matched ? "PASS" : "SKIP"),
          c.reason ? h("span", { class: "muted", style: "margin-left:6px" }, c.reason) : null));
      drawer(`试投结果 · ${r.name}`,
        note("用**模拟事件**（S1 紧急、标签 host=demo-host / env=prod）跑一遍筛选条件——不真发通知。"),
        ...rows);
    } catch (err) { toast(`试投失败：${err.message}`, "error"); }
  }

  function tagRowsEditor(initial) {
    // 标签/属性筛选行编辑器：[{k,op,v}] → 行数组 + 取值函数
    const wrap = h("div", {});
    const rows = [];
    function addRow(f = { k: "", op: "==", v: "" }) {
      const k = h("input", { class: "vs-input mono", placeholder: "键（如 env）", value: f.k || "", style: "width:120px" });
      const op = select(OPS, f.op || "==");
      const v = h("input", { class: "vs-input mono", placeholder: "值（如 prod）", value: f.v ?? "", style: "width:140px" });
      const row = h("div", { class: "form-row", style: "gap:6px" }, k, op, v,
        h("button", { class: "btn ghost", onclick: () => { rows.splice(rows.indexOf(r), 1); row.remove(); } }, "×"));
      const r = { k, op, v };
      rows.push(r);
      wrap.append(row);
    }
    (initial || []).forEach(addRow);
    return {
      el: h("div", {}, wrap, h("button", { class: "btn ghost", onclick: () => addRow() }, "+ 加一条")),
      value: () => rows.map((r) => ({ k: r.k.value.trim(), op: r.op.value, v: r.v.value.trim() }))
                       .filter((r) => r.k),
    };
  }

  function timeRangeEditor(initial) {
    const wrap = h("div", {});
    const rows = [];
    function addRow(f = { weekdays: [0,1,2,3,4,5,6], start: "00:00", end: "23:59" }) {
      const wd = h("div", { class: "row-actions", style: "margin:2px 0" },
        WDAYS.map(([v, label]) => {
          const b = h("button", { class: `pick-chip${(f.weekdays || []).includes(Number(v)) ? " active" : ""}`,
                                type: "button", style: "height:26px; padding:0 9px" }, label);
          b.addEventListener("click", () => b.classList.toggle("active"));
          b.dataset.day = v;
          return b;
        }));
      const start = h("input", { class: "vs-input mono", value: f.start || "00:00", style: "width:80px", placeholder: "00:00" });
      const end = h("input", { class: "vs-input mono", value: f.end || "23:59", style: "width:80px", placeholder: "23:59" });
      const row = h("div", { class: "form-row", style: "gap:6px" }, wd, start, end,
        h("button", { class: "btn ghost", onclick: () => { rows.splice(rows.indexOf(r), 1); row.remove(); } }, "×"));
      const r = { wd, start, end };
      rows.push(r);
      wrap.append(row);
    }
    (initial && initial.length ? initial : []).forEach(addRow);
    return {
      el: h("div", {}, wrap, h("button", { class: "btn ghost", onclick: () => addRow() }, "+ 加一组")),
      value: () => rows.map((r) => ({
        weekdays: [...r.wd.querySelectorAll("button")].filter((b) => b.classList.contains("active")).map((b) => Number(b.dataset.day)),
        start: r.start.value.trim(), end: r.end.value.trim(),
      })),
    };
  }

  function configEditor(initial, chans) {
    const channel = select([["", "（选择通知渠道）"]].concat(chans.map((c) => [String(c.id), c.name])),
                           String(initial?.channel_id || ""));
    const sevBox = h("div", { class: "row-actions" },
      SEV_OPTS.map(([v, label]) => {
        // D169-c：可选 chip 专用样式（.pick-chip），与动作按钮明确区分；
        // 选中态用级别色，与告警徽章同语义
        const b = h("button", { class: `pick-chip sev-${v}${(initial?.severities || []).includes(Number(v)) ? " active" : ""}`,
                                type: "button" }, label);
        b.addEventListener("click", () => b.classList.toggle("active"));
        b.dataset.sev = v;
        return b;
      }));
    const sevNote = h("span", { class: "muted", style: "font-size:0.74rem;margin-left:8px" }, "全不选 = 该条配置停用");
    const timeEd = timeRangeEditor(initial?.time_ranges);
    const labelEd = tagRowsEditor(initial?.label_keys);
    const targets = h("input", { class: "vs-input", value: (initial?.targets || []).map((t) => t.name || t).join(","),
                                 placeholder: "接收团队，逗号分隔（如 DBA-值班, SRE）" });
    return {
      el: h("div", { class: "card", style: "margin-bottom:10px" },
        h("div", { class: "grid-2" }, h("div", {}, reqLabel("通知渠道"), channel),
          h("div", {}, reqLabel("适用级别"), sevBox, sevNote)),
        h("div", { class: "form-row" }, h("label", {}, "适用时段（北京时间，留空 = 不限制）"), timeEd.el),
        h("div", { class: "form-row" }, h("label", {}, "适用标签（留空 = 不限制）"), labelEd.el),
        h("div", { class: "form-row" }, h("label", {}, helpTip("接收团队", "媒介是群机器人时消息进群；要 @ 到人请在渠道的消息模板里配。")), targets)),
      value: () => ({
        channel_id: Number(channel.value || 0),
        template_id: 0,
        severities: [...sevBox.querySelectorAll("button")].filter((b) => b.classList.contains("active")).map((b) => Number(b.dataset.sev)),
        time_ranges: timeEd.value(),
        label_keys: labelEd.value(),
        attributes: [],
        targets: targets.value.trim() ? targets.value.split(",").map((s) => ({ type: "team", name: s.trim() })).filter((t) => t.name) : [],
      }),
    };
  }

  function pipelineEditor(initial) {
    const wrap = h("div", {});
    const rows = [];
    const KINDS = [["set_tag", "设置标签"], ["del_tag", "删除标签"], ["set_annotation", "写注解"], ["drop", "丢弃事件"]];
    function addRow(f = { processor: "set_tag", key: "", value: "" }) {
      const kind = select(KINDS, f.processor || "set_tag");
      const key = h("input", { class: "vs-input mono", placeholder: "标签键", value: f.key || "", style: "width:120px" });
      const value = h("input", { class: "vs-input mono", placeholder: "值（drop 时忽略）", value: f.value || "", style: "width:140px" });
      const row = h("div", { class: "form-row", style: "gap:6px" }, kind, key, value,
        h("button", { class: "btn ghost", onclick: () => { rows.splice(rows.indexOf(r), 1); row.remove(); } }, "×"));
      const r = { kind, key, value };
      rows.push(r);
      wrap.append(row);
    }
    (initial || []).forEach(addRow);
    return {
      el: h("div", {},
        note("Pipeline 在**事件产生时、屏蔽之前**执行：改标签会影响事件身份（不同维度各自成事件）；「丢弃事件」= 不产生、不落库、不通知。"),
        wrap, h("button", { class: "btn ghost", onclick: () => addRow() }, "+ 加一步")),
      value: () => rows.map((r) => ({ processor: r.kind.value, key: r.key.value.trim(), value: r.value.value.trim() }))
                       .filter((r) => r.processor !== "set_tag" && r.processor !== "set_annotation" ? true : r.key),
    };
  }

  function ruleEditor(r) {
    const editing = !!r;
    const name = h("input", { class: "vs-input", value: r?.name || "", placeholder: "如 DBA-Postgres 紧急" });
    const desc = h("input", { class: "vs-input", value: r?.description || "", placeholder: "备注（可选）" });
    let chans = [];
    try { chans = []; } catch { /* filled below */ }
    const cfgWrap = h("div", {});
    const cfgs = [];

    (async () => {
      try { chans = (await api.get("/api/ops/notify-channels")).items || []; } catch { /* 降级为空 */ }
      const addCfg = (c) => { const ed = configEditor(c, chans); cfgs.push(ed); cfgWrap.append(ed.el); };
      (r?.configs || []).forEach(addCfg);
      if (!cfgs.length) addCfg(null);
      const addBtn = h("button", { class: "btn ghost", onclick: () => addCfg(null) }, "+ 加一条通知配置");
      cfgWrap.append(addBtn);
    })();

    const pipeEd = pipelineEditor(r?.pipelines || []);

    drawer(editing ? `编辑通知规则 · ${r.name}` : "新增通知规则",
      h("div", { class: "note" }, "一条规则 = 若干条通知配置。事件按顺序匹配：命中即按该配置投递（同规则内多配置可都投）。"),
      h("div", { class: "grid-2" }, h("div", {}, reqLabel("名称"), name), h("div", {}, "备注", desc)),
      h("h3", {}, "通知配置"),
      cfgWrap,
      h("h3", {}, "事件 Pipeline（可选）"),
      pipeEd.el,
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        const bad = needText(name.value, "规则名称", 128);
        if (bad) { toast(bad, "error"); name.focus(); return; }
        const cfgVals = cfgs.map((c) => c.value());
        if (!cfgVals.length) { toast("至少一条通知配置", "error"); return; }
        if (cfgVals.some((c) => !c.channel_id)) { toast("每条通知配置都要选通知渠道", "error"); return; }
        const payload = { name: name.value.trim(), description: desc.value.trim(), enabled: editing ? r.enabled : true,
                          configs: cfgVals, pipelines: pipeEd.value() };
        try {
          await busy(e.currentTarget, "保存中…", () => editing
            ? api.put(`/api/ops/notify-rules/${r.id}`, payload)
            : api.post("/api/ops/notify-rules", payload));
          toast("已保存。建议用「试投」验证筛选条件", "ok"); closeDrawer(); draw();
        } catch (err) { toast(`失败：${err.message}`, "error"); }
      } }, "保存并返回")));
  }

  // ───────────────────────── 订阅 ─────────────────────────
  async function drawSubs() {
    let items = [], rules = [];
    try { items = (await api.get("/api/ops/alert-subscribes")).items || []; } catch (e) { body.replaceChildren(h("div", { class: "empty" }, e.message)); return; }
    try { rules = (await api.get("/api/ops/alert-rules", { page_size: 200 })).items || []; } catch { /* 降级 */ }
    const ruleName = (id) => (rules.find((x) => x.id === id) || {}).name || `规则#${id}`;
    fill(body,
      note("订阅 = 「我也想收到这类告警」。由订阅方自己配、自己选渠道；**不需要**改被订阅的规则。"),
      table([
        { title: "订阅的规则", render: (r) => ruleName(r.rule_id) },
        { title: "名称", render: (r) => r.name || "-" },
        { title: "状态", width: "76px", render: (r) => badge(r.enabled ? "启用" : "停用", r.enabled ? "PASS" : "SKIP") },
        { title: "筛选", render: (r) => {
            const f = r.severity_filter || {};
            return (f.severities || []).length ? badge("S" + f.severities.join("/S"), "ACCENT") : h("span", { class: "muted" }, "全部级别");
          } },
        { title: "操作", width: "110px", render: (r) => h("div", { class: "row-actions" },
            canWrite ? h("button", { class: "btn ghost", onclick: () => subEditor(r) }, "编辑") : null,
            canWrite ? h("button", { class: "btn ghost", onclick: async (e) => {
              try { await busy(e.currentTarget, "删除中…", () => api.del(`/api/ops/alert-subscribes/${r.id}`)); toast("已删除", "ok"); }
              catch (err) { toast(`失败：${err.message}`, "error"); }
              draw(); } }, "删除") : null) },
      ], items, { emptyText: "暂无订阅。订阅 = 「我也想收到这类告警」：选一条告警规则、"
          + "勾只收的级别、填自己的收件渠道 ID 即可——被订阅的规则不需要任何改动。" }));
  }

  function subEditor(s) {
    const editing = !!s;
    const name = h("input", { class: "vs-input", value: s?.name || "", placeholder: "订阅名（可选）" });
    const ruleId = h("input", { class: "vs-input mono", value: s?.rule_id ?? "", placeholder: "要订阅的告警规则 ID（规则列表可见）" });
    const sevBox = h("div", { class: "row-actions" },
      SEV_OPTS.map(([v, label]) => {
        const b = h("button", { class: `btn ghost${((s?.severity_filter || {}).severities || []).includes(Number(v)) ? " active" : ""}`,
                                style: "padding:2px 10px" }, label);
        b.addEventListener("click", () => b.classList.toggle("active"));
        b.dataset.sev = v;
        return b;
      }));
    const chanId = h("input", { class: "vs-input mono", value: (s?.config || {}).channel_id ?? "", placeholder: "收到的通知渠道 ID" });
    drawer(editing ? `编辑订阅 · ${s.name || s.id}` : "新增订阅",
      h("div", { class: "note" }, "订阅方自己选渠道与级别；被订阅的规则不需要任何改动。"),
      h("div", { class: "grid-2" }, h("div", {}, "订阅名", name), h("div", {}, reqLabel("告警规则 ID"), ruleId)),
      h("div", { class: "form-row" }, h("label", {}, "只收这些级别（全不选 = 全部）"), sevBox),
      h("div", { class: "form-row" }, h("label", {}, reqLabel("收件渠道 ID")), chanId),
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        const rid = Number(ruleId.value || 0);
        if (!rid) { toast("请填告警规则 ID", "error"); ruleId.focus(); return; }
        const sev = [...sevBox.querySelectorAll("button")].filter((b) => b.classList.contains("active")).map((b) => Number(b.dataset.sev));
        const payload = { rule_id: rid, name: name.value.trim(), enabled: editing ? s.enabled : true,
                          severity_filter: { severities: sev }, config: { channel_id: Number(chanId.value || 0) } };
        try {
          await busy(e.currentTarget, "保存中…", () => editing
            ? api.put(`/api/ops/alert-subscribes/${s.id}`, payload)
            : api.post("/api/ops/alert-subscribes", payload));
          toast("已保存", "ok"); closeDrawer(); draw();
        } catch (err) { toast(`失败：${err.message}`, "error"); }
      } }, "保存并返回")));
  }

  // ───────────────────────── 业务组 ─────────────────────────
  async function drawGroups() {
    let items = [];
    try { items = (await api.get("/api/ops/busi-groups")).items || []; } catch (e) { body.replaceChildren(h("div", { class: "empty" }, e.message)); return; }
    fill(body,
      h("div", { class: "note" }, "业务组 = 主机归属 + 路由标签（名字带层级，如 DBA/MySQL）。本轮不作为数据可见性的权限边界。"),
      table([
        { title: "名称", render: (r) => h("span", { class: "mono" }, r.name) },
        { title: "说明", render: (r) => r.description || "-" },
        { title: "成员团队", render: (r) => (r.members || []).length
            ? h("div", { style: "font-size:0.78rem" }, ...(r.members || []).map((m) =>
                h("div", {}, `${m.team}（${m.permission === "write" ? "读写" : "只读"}）`)))
            : h("span", { class: "muted" }, "无") },
        { title: "操作", width: "200px", render: (r) => h("div", { class: "row-actions" },
            canWrite ? h("button", { class: "btn ghost", onclick: () => groupEditor(r) }, "编辑") : null,
            canWrite ? h("button", { class: "btn ghost", onclick: () => memberEditor(r) }, "成员") : null,
            canWrite ? h("button", { class: "btn ghost", onclick: async (e) => {
              if (!window.confirm(`删除业务组「${r.name}」？其成员关系一并删除。`)) return;
              try { await busy(e.currentTarget, "删除中…", () => api.del(`/api/ops/busi-groups/${r.id}`)); toast("已删除", "ok"); }
              catch (err) { toast(`失败：${err.message}`, "error"); }
              draw(); } }, "删除") : null) },
      ], items, { emptyText: "暂无业务组。业务组用于主机归属与通知路由（名字用 / 表层级，如 DBA/MySQL）；"
          + "本轮不作为数据可见性的权限边界。" }));
  }

  function groupEditor(g) {
    const editing = !!g;
    const name = h("input", { class: "vs-input mono", value: g?.name || "", placeholder: "DBA/MySQL（用 / 表层级）" });
    const desc = h("input", { class: "vs-input", value: g?.description || "", placeholder: "说明（可选）" });
    drawer(editing ? `编辑业务组 · ${g.name}` : "新增业务组",
      h("div", { class: "grid-2" }, h("div", {}, reqLabel("名称"), name), h("div", {}, "说明", desc)),
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        const bad = needText(name.value, "业务组名称", 128);
        if (bad) { toast(bad, "error"); name.focus(); return; }
        const payload = { name: name.value.trim(), description: desc.value.trim() };
        try {
          await busy(e.currentTarget, "保存中…", () => editing
            ? api.put(`/api/ops/busi-groups/${g.id}`, payload)
            : api.post("/api/ops/busi-groups", payload));
          toast("已保存", "ok"); closeDrawer(); draw();
        } catch (err) { toast(`失败：${err.message}`, "error"); }
      } }, "保存并返回")));
  }

  function memberEditor(g) {
    const team = h("input", { class: "vs-input", placeholder: "团队名" });
    const perm = select([["read", "只读"], ["write", "读写"]], "read");
    const list = h("div", {}, ...(g.members || []).map((m) =>
      h("div", { class: "form-row" }, h("label", {}, `${m.team}（${m.permission === "write" ? "读写" : "只读"}）`),
        h("button", { class: "btn ghost", onclick: async (e) => {
          try { await busy(e.currentTarget, "移除中…", () => api.del(`/api/ops/busi-groups/${g.id}/members/${m.id}`)); toast("已移除", "ok"); closeDrawer(); draw(); }
          catch (err) { toast(`失败：${err.message}`, "error"); }
        } }, "移除"))));
    drawer(`业务组成员 · ${g.name}`,
      list.length ? list : h("div", { class: "note" }, "暂无成员团队"),
      h("div", { class: "grid-2" }, h("div", {}, reqLabel("新增团队"), team), h("div", {}, "权限", perm)),
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        if (!team.value.trim()) { toast("请填团队名", "error"); team.focus(); return; }
        try {
          await busy(e.currentTarget, "添加中…", () => api.post(`/api/ops/busi-groups/${g.id}/members`,
            { team: team.value.trim(), permission: perm.value }));
          toast("已添加", "ok"); closeDrawer(); draw();
        } catch (err) { toast(`失败：${err.message}`, "error"); }
      } }, "添加")));
  }

  setTab("rules");
}
