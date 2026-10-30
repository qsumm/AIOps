// pages/runbooks.js —— 处置与变更（FR-F ops-runbooks）
// 必备：变更单列表（版本号）、详情五段渲染、执行按钮（三模式）、审计时间线
import { api, getUser, can } from "../api.js";
import { h, table, badge, drawer, toast, fmtTime, select, fmtTime as ft, note, copyText, busy, reqLabel, needText, statusBadge, helpTip, pager } from "../ui.js";

const RISK = { low: ["低风险", "PASS"], mid: ["中风险", "WARN"], high: ["高风险", "FAIL"] };
// D105：表单行原语（cases/rules/services 各自持有同名局部函数，本页此前缺失——
// 点击「新建变更单」直接 ReferenceError，抽屉弹不出来，表现为"按钮没反应"）。
function frow(label, el) { return h("div", { class: "form-row" }, h("label", {}, label), el); }
// 执行方式三选（FR-E2）：只给建议 / 人工确认 / 白名单自动（仅命中动作清单的动作免确认）
const MODE_BADGE = {
  suggest: ["仅建议", "SKIP"],
  manual: ["人工确认", "WARN"],
  auto: ["白名单自动", "ACCENT"],
};
const MODE_LABEL = { suggest: "仅建议（只给建议，不生成执行指令）", manual: "人工确认（每次执行需确认）", auto: "白名单自动（命中动作清单才免确认）" };

export async function render(root, sub = "") {
  const me = getUser() || {};
  const canWrite = can("write");       // 处置执行/改单：admin 专属
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  const head = h("div", { class: "page-head" },
    h("div", { class: "actions" },
      h("span", { class: "muted", style: "font-size:0.78rem" }, "变更单由「问题看板 → 一键诊断 → 生成变更单」产生"),
      h("button", { class: "btn secondary", onclick: draw }, "刷新"),
      canWrite ? h("button", { class: "btn", onclick: openCreate }, "新建变更单") : null));
  const body = h("div", { id: "rb-body" });
  const state = { page: 1, pageSize: 20 };   // D128
  const pagerEl = h("div", { class: "pager" });
  root.replaceChildren(head, body, pagerEl)

  async function draw() {
    let d;
    try { d = await api.get("/api/ops/runbooks", { page: state.page, page_size: state.pageSize }); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    body.replaceChildren(table([
      { title: "序号", width: "46px", render: (_r, i) => h("span", { class: "mono muted" }, String(i + 1)) },   // D129
      { title: "变更单", render: (r) => h("span", {}, r.name) },
      { title: "关联事件", width: "92px", render: (r) => h("span", { class: "mono muted" }, r.incident_id ? `#${r.incident_id}` : "-") },
      { title: "主机", width: "120px", render: (r) => h("span", { class: "mono muted" }, (r.host_ident || "").slice(0, 10) || "-") },
      { title: "风险", width: "88px", render: (r) => badge(...(RISK[r.risk_level] || ["-", "SKIP"])) },
      { title: "版本", width: "66px", render: (r) => h("span", { class: "mono" }, `v${r.version}`) },
      { title: "步骤", width: "66px", render: (r) => h("span", { class: "mono" }, r.steps) },
      { title: "状态", width: "88px", render: (r) => statusBadge(r.status, r.status === "aborted" ? "FAIL" : undefined) },
      { title: "执行方式", width: "104px", render: (r) => badge(...MODE_BADGE[r.execution_mode || (r.whitelist_enabled ? "auto" : "manual")]) },
      { title: "创建", width: "110px", render: (r) => h("span", { class: "muted" }, fmtTime(r.created_at)) },
    ], d.items || [], { emptyText: "暂无变更单——先在问题看板完成一次诊断，再点「生成变更单」", onRowClick: (r) => detail(r.id) }));
    pagerEl.replaceChildren(pager({ total: d.total || 0, page: state.page, pageSize: state.pageSize, unit: "张",
      onChange: (p, ps) => { state.page = p; if (ps !== state.pageSize) state.pageSize = ps; draw(); } }));
    pagerEl.replaceChildren(pager({ total: d.total || 0, page: state.page, pageSize: state.pageSize,
      onChange: (p, ps) => { state.page = p; if (ps !== state.pageSize) state.pageSize = ps; draw(); } }));
  }

  async function detail(rid) {
    const d = await api.get(`/api/ops/runbooks/${rid}`);
    const panel = drawer(`变更单 v${d.version} · ${d.name}`,
      h("div", { class: "detail-meta" },
        kv("风险等级", badge(...(RISK[d.risk_level] || ["-", "SKIP"]))),
        kv("状态", badge(d.status, d.status === "done" ? "PASS" : "RUNNING")),
        kv("目标主机", h("span", { class: "mono" }, d.host_ident || "-")),
        kv("关联事件", d.incident_id ? h("a", { href: `#/incidents`, class: "cite" }, `#${d.incident_id}`) : "-")),
      h("div", { class: "card" }, h("h3", {}, "1. 风险评估"), h("div", { class: "rb-text" }, d.risk_assessment || "-")),
      h("div", { class: "card" }, h("h3", {}, "2. 前置检查"), h("div", { class: "rb-text" },
        ...(d.pre_checks || []).map((p) => h("div", {}, `- ${p.desc}`, h("span", { class: "mono muted", style: "margin-left:8px" }, `（${p.check}）`))))),
      h("div", { class: "card" }, h("h3", {}, `3. 执行步骤（${(d.steps || []).length}）`),
        table([
          { title: "#", width: "42px", render: (r) => h("span", { class: "mono muted" }, r.idx) },
          { title: "描述", render: (r) => r.desc },
          { title: "验证/命令", width: "230px", render: (r) => h("span", { class: "mono", style: "font-size:0.74rem" }, r.check || r.cmd || "-") },
          { title: "模式", width: "82px", render: (r) => badge(r.mode === "readonly" ? "只读" : "需审批", r.mode === "readonly" ? "PASS" : "WARN") },
          { title: "操作", width: "132px", render: (r) => {
              const needConfirm = r.mode !== "readonly" && (d.execution_mode || "manual") !== "suggest";
              if (!canWrite) return h("span", { class: "muted" }, "只读");
              if (r.mode === "readonly") return h("button", { class: "btn ghost", onclick: (e) => execReadonly(rid, r, e.currentTarget) }, "执行（只读）");
              return needConfirm
                ? h("button", { class: "btn ghost", onclick: () => confirmCard(rid, r, d) }, "确认并执行")
                : h("span", { class: "muted", style: "font-size:0.74rem" }, "仅建议模式");
            } },
        ], d.steps || [])),
      h("div", { class: "card" }, h("h3", {}, "4. 回滚方案"), h("div", { class: "rb-text" }, d.rollback_plan || "-")),
      h("div", { class: "card" }, h("h3", {}, "5. 验证方式"), h("div", { class: "rb-text" }, d.verification || "-")),
      h("div", { class: "card" }, h("h3", {}, "版本时间线"),
        table([
          { title: "版本", width: "70px", render: (r) => h("span", { class: "mono" }, `v${r.version}`) },
          { title: "说明", render: (r) => r.summary },
          { title: "时间", width: "150px", render: (r) => h("span", { class: "muted mono" }, fmtTime(r.created_at)) },
        ], d.versions || [])),
      h("div", { class: "card" }, h("h3", {}, "审计时间线（护栏判定）"),
        table([
          { title: "动作", width: "82px", render: (r) => h("span", { class: "mono" }, r.action) },
          { title: "护栏", width: "110px", render: (r) => badge(r.guardrail || "-", r.guardrail === "denylist" ? "FAIL" : r.guardrail === "allowlist" ? "PASS" : "WARN") },
          { title: "结果", render: (r) => h("span", { class: "muted" }, r.result || "-") },
          { title: "操作者", width: "80px", render: (r) => r.operator },
          { title: "时间", width: "150px", render: (r) => h("span", { class: "muted mono" }, fmtTime(r.created_at)) },
        ], d.audits || [], { emptyText: "暂无执行记录" })),
      canWrite ? h("div", { class: "detail-actions" },
        modeSelect(rid, d),
        h("button", { class: "btn", onclick: async (e) => {
          try { await busy(e.currentTarget, "提交中…", () => api.put(`/api/ops/runbooks/${rid}`, { status: "done", summary: "标记完成" }));
            toast("已标记完成", "ok"); close(); draw(); } catch (err) { toast(`失败：${err.message}`, "error"); }
        } }, "标记完成")) : null);
    return panel;
  }

  /** 手动新建变更单（FR-E1「手动创建」半边）：步骤按行填写，`描述 | 命令`，只读命令自动免确认。 */
  function openCreate() {
    const name = h("input", { class: "vs-input", placeholder: "例如：清理 /data 临时文件" });
    const host = h("input", { class: "vs-input mono", placeholder: "目标主机 ident 或主机名（可留空）" });
    const risk = select([["low", "低风险"], ["mid", "中风险"], ["high", "高风险"]], "mid");
    const mode = select([["manual", "人工确认"], ["auto", "白名单自动"], ["suggest", "仅建议"]], "manual");
    const stepsBox = h("textarea", { class: "vs-input mono", rows: "9",
      placeholder: "每行一步：描述 | 命令\n看磁盘 | df -h\n清理临时文件 | find /tmp -type f -mtime +7 -delete" });
    const rollback = h("textarea", { class: "vs-input", rows: "4", placeholder: "回滚方式（可留空，多行）" });
    drawer("新建变更单",
      note("五段中的「前置检查/验证口径」按命令自动归类；含 `df/free/ps/systemctl status` 等只读命令的步骤会**免确认**直接放行，其余按执行方式判定（人工确认会弹确认卡）。"),
      h("div", { class: "grid-2" }, frow(reqLabel("名称"), name), frow("目标主机", host), frow(["风险等级", helpTip("用于审批与展示的定级（S1~S3），不改变护栏判定；诊断报告生成的变更单会按其严重度带出来。")], risk), frow(["执行方式", helpTip("只读步骤（df / free / ps / systemctl status 等）**免确认**直接放行；写操作按这里的选择：仅建议=只给建议不执行、人工确认=每次弹确认卡、白名单自动=仅命中动作清单的动作免确认。")], mode)),
      frow([reqLabel("步骤（每行：描述 | 命令）"), helpTip("每行一条：`描述 | 命令`。命令会按「只读 / 写操作」自动分类——只读的免审批，写操作需要审批并留痕。")], stepsBox), frow(["回滚方式", helpTip("出问题时怎么退回。护栏的「无回滚不执行」原则：没有回滚方案的写操作不会被自动执行。")], rollback),
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        const READONLY = /^(df|free|top|ps|uptime|cat|tail|head|ls|grep|ss|netstat|du|vmstat|iostat|nproc|uname|systemctl status|systemctl is-active|journalctl|docker ps|docker logs|openssl s_client|curl -I)\b/;
        const steps = (stepsBox.value || "").split("\n").map((l) => l.trim()).filter(Boolean).map((l, i) => {
          const [desc, ...rest] = l.split("|");
          const cmd = rest.join("|").trim();
          return { idx: i + 1, desc: (desc || "").trim() || cmd, cmd,
                   mode: READONLY.test(cmd) ? "readonly" : "approval",
                   check: READONLY.test(cmd) ? cmd : "",
                   rollback: rollback.value || "" };
        });
        // D59：名称/步骤必填（原判定保留）+ 名称超长对齐 DB 列宽 128
        const bad = needText(name.value, "变更单名称", 128) || (steps.length ? "" : "至少填写一条步骤");
        if (bad) return toast(bad, "warn");
        try {
          const r = await busy(e.currentTarget, "创建中…", () => api.post("/api/ops/runbooks", { name: name.value.trim(), host_ident: host.value,
            risk_level: risk.value, execution_mode: mode.value, steps, rollback_plan: rollback.value,
            risk_assessment: `手动创建，风险等级 ${risk.value}` }));
          toast(`已创建变更单 #${r.id}（${r.steps} 步）`, "ok"); close(); await draw(); detail(r.id);   // D61：建完直接看详情，别让用户回列表里找
        } catch (e) { toast(`创建失败：${e.message}`, "error"); }
      } }, "创建"), h("button", { class: "btn ghost", onclick: () => close() }, "取消")));
  }

  async function execReadonly(rid, step, btn) {
    try {
      const r = await busy(btn, "执行中…", () => api.post(`/api/ops/runbooks/${rid}/execute`, { step_idx: step.idx }));
      draw();
      showCommand(r, step.idx);
    } catch (e) { toast(`失败：${e.message}`, "error"); }
  }

  /** 确认卡：把"要执行什么、影响是什么、怎么回滚"摆在一屏，用户确认后才签发凭据并执行。 */
  async function confirmCard(rid, step, rb) {
    // 先取一次凭据以拿到"是否命中动作清单"（前端据此决定是直接执行还是必须人工确认）
    let pre;
    try { pre = await api.post(`/api/ops/runbooks/${rid}/steps/${step.idx}/confirm`); }
    catch (e) {
      if (String(e.message || "").includes("仅建议")) return toast("仅建议模式：请人工在目标机执行", "warn");
      return toast(`无法发起确认：${e.message}`, "error");
    }
    if (!pre.allowlisted && !pre.confirm_token) return toast("未签发凭据，请检查执行方式", "error");
    const cmd = pre.cmd || step.check || step.cmd || "";
    const isAgent = Boolean(step.action);
    const p = drawer(`确认执行 · 步骤 ${step.idx}`,
      note(isAgent
        ? "确认后平台将通过 **Agent 任务通道** 把动作下发到目标机执行，task_id 与真实回传结果会写入审计。本次确认会留痕（谁/何时/哪一步）。"
        : "平台**不代执行**原始命令：确认后仅生成待执行命令，由你在目标机执行并回填结果。本次确认会留痕（谁/何时/哪一步）。"),
      h("div", { class: "card" }, h("h3", {}, "动作"),
        h("div", { class: "rb-text" }, pre.desc || step.desc || "-"),
        h("div", { class: "muted", style: "margin-top:6px" }, `目标主机：${pre.host_ident || rb.host_ident || "-"}`)),
      h("div", { class: "card" }, h("h3", {}, isAgent ? "Agent 将执行的动作" : "待执行命令（原文）"),
        h("pre", { class: "json-block", style: "white-space:pre-wrap" }, cmd || "-")),
      h("div", { class: "grid-2" },
        h("div", { class: "card" }, h("h3", {}, "风险等级"), h("div", {}, badge(...(RISK[pre.risk_level] || ["-", "SKIP"])))),
        h("div", { class: "card" }, h("h3", {}, "回滚方式"), h("div", { class: "rb-text" },
          (pre.rollback || "见变更单第 4 段").slice(0, 400)))),
      pre.allowlisted
        ? note(`该动作命中官方动作清单（${pre.allowlist_id}）：可直接执行`)
        : note("该动作**未命中**动作清单，因此即便在「白名单自动」模式下也需要本次人工确认。"),
      h("div", { class: "detail-actions" },
        h("button", { class: "btn", onclick: async (e) => {
          try {
            const r = await busy(e.currentTarget, isAgent ? "下发中…" : "生成中…",
              () => api.post(`/api/ops/runbooks/${rid}/execute`,
                { step_idx: step.idx, confirm_token: pre.confirm_token }));
            close();
            draw();   // 只刷新列表；不能再 detail(rid)，否则会把结果抽屉立刻覆盖
            if (r.executed_via === "agent") showAgentResult(r, step);
            else showCommand(r, step.idx);
          } catch (err) { toast(`执行失败：${err.message}`, "error"); }
        } }, isAgent ? "我已确认风险，确认执行" : "我已确认风险，生成待执行命令"),
        h("button", { class: "btn ghost", onclick: () => close() }, "取消")));
    return p;
  }

  /** Agent 真实执行结果（结构化动作）：展示 task_id / rc / stdout / stderr，而不是让用户复制命令。 */
  function showAgentResult(r, step) {
    const ok = r.ok !== false;
    const result = r.agent_result ?? {};
    drawer(`Agent 执行${ok ? "成功" : "失败"} · 步骤 ${step.idx}`,
      note(`平台已通过 Agent 任务通道下发「${r.action || step.action || "-"}」到目标主机；以下为 Agent 真实回传结果（task_id: ${r.task_id || "-"}）。`),
      h("div", { class: "card" }, h("h3", {}, ok ? "执行结果" : "失败原因"),
        h("pre", { class: "json-block", style: "white-space:pre-wrap;max-height:360px" },
          r.error || JSON.stringify(result, null, 2) || "-")),
      h("div", { class: "detail-actions" },
        h("button", { class: "btn ghost", onclick: () => close() }, "关闭")));
  }

  function showCommand(r, idx) {
    drawer(`执行已放行 · 步骤 ${idx}`,
      note("V1 无远程命令通道：请在目标机执行下列命令并回填结果（本次操作已入审计）。"),
      h("div", { class: "copy-row" }, h("input", { class: "vs-input mono", readonly: "readonly", value: r.execute_on_target || "" }),
        h("button", { class: "btn secondary", onclick: () => copyText(r.execute_on_target || "") }, "复制")),
      h("div", { class: "muted" }, `护栏判定：${r.guardrail}　审批方式：${r.approval_mode || "-"}` +
        (r.approved_by ? `　确认人：${r.approved_by}` : "") + `　目标主机：${r.host}`));
  }

  /** 执行方式下拉（三模式）：改完落库并刷新抽屉。 */
  function modeSelect(rid, d) {
    const sel = select([["manual", "人工确认"], ["auto", "白名单自动"], ["suggest", "仅建议"]],
      d.execution_mode || (d.whitelist_enabled ? "auto" : "manual"), { title: "执行方式（FR-E2 三选）" });
    sel.addEventListener("change", async () => {
      const v = sel.value;
      sel.disabled = true;   // D64：请求期间锁定（连续快速切换会产生多个 PUT 竞态，最后落库的可能是先发的）
      try {
        await api.put(`/api/ops/runbooks/${rid}`, { execution_mode: v, summary: `执行方式改为${MODE_LABEL[v]}` });
        toast(`已改为${MODE_LABEL[v]}`, "ok"); close(); detail(rid); draw();
      } catch (e) { toast(`切换失败：${e.message}`, "error"); }
      finally { sel.disabled = false; }
    });
    return sel;
  }

  function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v)); }
  const close = () => document.querySelector(".detail-head .btn")?.click();
  await draw();
  // 深链：/#/runbooks/22 直接打开变更单详情
  if (/^\d+$/.test(String(sub || "").trim())) {
    try { await detail(Number(sub)); }
    catch { toast("变更单不存在或已删除", "error"); }
  }
}

export function destroy() {}
