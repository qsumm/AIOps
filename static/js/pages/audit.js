// pages/audit.js —— 操作审计（FR-F ops-audit）
// 必备：按时间/操作者/动作检索（GET /api/audit-logs 分页）、详情抽屉（前后值摘要/IP）、demo 角色脱敏只读
import { api, getUser } from "../api.js";
import { h, table, badge, drawer, toast, fmtTime, select, pager as pagerUI } from "../ui.js";

const ACTION_LABEL = {
  login: "登录", rule_create: "新建规则", rule_update: "修改规则", rule_delete: "停用规则",
  rule_enable: "启用规则", rule_disable: "停用规则", rule_purge: "删除规则",
  mute_create: "新建抑制", mute_delete: "解除抑制", alert_ack: "确认告警", alert_resolve: "解决告警",
  alert_bulk_ack: "批量确认", alert_bulk_resolve: "批量解决",
  incident_create: "新建事件", incident_ack: "确认事件", incident_merge: "合并事件", incident_split: "拆分事件",
  incident_close: "关闭事件", incident_diagnose: "执行诊断", runbook_create: "新建变更单",
  runbook_update: "更新变更单", runbook_execute: "执行变更", runbook_confirm: "批准执行",
  runbook_confirm_denied: "批准被拒",
  inspection_create: "新建巡检", inspection_update: "修改巡检", inspection_delete: "删除巡检",
  prompt_ds_create: "新建外部指标源",
  prom_ds_create: "新建数据源", prom_ds_update: "修改数据源", prom_ds_delete: "删除数据源",
  license_activate: "激活 License", channel_create: "新建通知渠道", channel_update: "修改通知渠道",
  channel_delete: "删除通知渠道", host_rename: "主机改名", password_change: "修改密码",
  skill_check: "技能检查",
  case_card_update: "修改案例卡", case_card_delete: "删除案例卡",
  service_create: "登记服务", service_update: "修改服务", service_delete: "删除服务",
  heal_dispatch: "下发恢复", heal_denied: "恢复被拒（护栏）", heal_kill_switch: "切换自愈总开关", heal_reset: "重置恢复状态",
  log_fetch_dispatch: "下发日志取证", perm_denied: "越权尝试（已拦截）",
};

export async function render(root) {
  const me = getUser() || {};
  const isDemo = me.role === "demo";
  const state = { page: 1, action: "", actor: "", date: "" };
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  // D96：筛选从"手输代码"改为下拉——选项由后端从审计表里取**真实存在**的操作者/动作码，
  // 首次 draw() 时回填；用户不再需要对着占位符猜"alert_ack"这类代码。
  const actorSel = select([["", "全部操作者"]], "", { onchange: (e) => { state.actor = e.target.value; state.page = 1; draw(); } });
  const actionSel = select([["", "全部动作"]], "", { onchange: (e) => { state.action = e.target.value; state.page = 1; draw(); } });
  function fillFacets(f) {
    if (!f) return;
    actorSel.replaceChildren(...[["", "全部操作者"], ...f.operators.map((o) => [o, o])]
      .map(([v, l]) => h("option", { value: v, selected: state.actor === v ? "" : null }, l)));
    actionSel.replaceChildren(...[["", "全部动作"], ...f.actions.map((a) => [a, ACTION_LABEL[a] ? `${ACTION_LABEL[a]}（${a}）` : a])]
      .map(([v, l]) => h("option", { value: v, selected: state.action === v ? "" : null }, l)));
  }

  const head = h("div", { class: "page-head" },
    h("div", { class: "actions" },
      actorSel,
      actionSel,
      h("input", { class: "vs-input", type: "date", title: "按北京时间（UTC+8）过滤", style: "width:150px", onchange: (e) => { state.date = e.target.value; state.page = 1; draw(); } }),
      h("button", { class: "btn secondary", onclick: () => { Object.assign(state, { page: 1, action: "", actor: "", date: "" }); render(root); } }, "重置")));
  const tip = h("div", { class: "note" }, isDemo
    ? "当前为演示访客（demo）：审计内容按脱敏只读展示（IP 与详情已隐藏）。"
    : "审计覆盖：登录（含失败尝试）、规则/抑制/渠道变更、告警处理、事件合并/拆分/诊断、变更单执行（含护栏判定）等。时间为北京时间（库内 UTC）。");
  const pager = h("div", { class: "pager" });
  const body = h("div", { id: "audit-body" });
  root.replaceChildren(head, tip, body, pager);

  async function draw() {
    let d;
    try {
      d = await api.get("/api/audit-logs", { page: state.page, page_size: state.pageSize || 30, action: state.action, actor: state.actor, date: state.date });
    } catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    fillFacets(d.facets);
    const items = d.items || [];
    body.replaceChildren(table([
      { title: "序号", width: "46px", render: (_r, i) => h("span", { class: "mono muted" }, String(i + 1)) },   // D129
      { title: "时间（北京时间）", width: "160px", render: (r) => h("span", { class: "mono muted" }, fmtTime(r.created_at)) },
      { title: "操作者", width: "110px", render: (r) => h("span", {}, r.username || `user#${r.user_id ?? "-"}`) },
      { title: "动作", width: "150px", render: (r) => badge(ACTION_LABEL[r.action] || r.action, "ACCENT") },
      { title: "对象", width: "170px", render: (r) => h("span", { class: "mono muted" }, r.resource || "-") },
      { title: "IP", width: "130px", render: (r) => h("span", { class: "mono muted" }, isDemo ? "***" : (r.ip || "-")) },
    ], items, { emptyText: "无审计记录", onRowClick: (r) => detail(r) }));
    pager.replaceChildren(pagerUI({ total: d.total || 0, page: state.page, pageSize: 30, unit: "条",
      onChange: (p) => { state.page = p; draw(); } }));

    pager.replaceChildren(pagerUI({ total: d.total || 0, page: state.page, pageSize: 30, unit: "条",
      onChange: (p) => { state.page = p; draw(); } }));
  }

  function detail(r) {
    const raw = r.detail || {};
    const redacted = isDemo ? "***" : JSON.stringify(raw, null, 2);
    drawer(`审计详情 · ${ACTION_LABEL[r.action] || r.action}`,
      h("div", { class: "detail-meta" },
        kv("时间（北京时间）", fmtTime(r.created_at)),
        kv("操作者", r.username || `user#${r.user_id ?? "-"}`),
        kv("动作", h("span", { class: "mono" }, r.action)),
        kv("对象", h("span", { class: "mono" }, r.resource || "-")),
        kv("IP", isDemo ? "***" : (r.ip || "-"))),
      h("div", { class: "card" }, h("h3", {}, isDemo ? "前后值摘要（脱敏）" : "前后值摘要"),
        h("pre", { class: "json-block" }, String(redacted).slice(0, 4000))));
  }

  function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v)); }
  await draw();
}

export function destroy() {}
