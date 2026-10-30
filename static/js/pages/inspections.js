// pages/inspections.js —— 巡检报告（FR-F ops-inspections）
// 必备：巡检任务列表（cron/目标/最近结果）、日报渲染、手动触发
import { api, getUser, can } from "../api.js";
import { marked } from "/vendor/marked.esm.js";
import { h, table, badge, drawer, toast, fmtTime, relTime, closeDrawer, busy, progress, reqLabel, needText, helpTip, pager } from "../ui.js";

const md = (t) => { try { return marked.parse(t || "", { breaks: true, gfm: true }); } catch { return String(t || ""); } };

export async function render(root) {
  const me = getUser() || {};
  const canRun = can("diag");          // 触发巡检：admin/member
  const canWrite = can("write");       // 新建/编辑/删除巡检任务：admin 专属
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  const head = h("div", { class: "page-head" },
    h("div", { class: "actions" },
      h("button", { class: "btn secondary", onclick: draw }, "刷新"),
      canRun ? h("button", { class: "btn", onclick: (e) => runOnce(e.currentTarget) }, "立即巡检一次") : null,
      canWrite ? h("button", { class: "btn", onclick: () => editor(null) }, "新建巡检") : null));
  const body = h("div", { id: "insp-body" });
  const state = { page: 1, pageSize: 20 };   // D128
  const pagerEl = h("div", { class: "pager" });
  root.replaceChildren(head, body, pagerEl)

  /* 新建/编辑巡检任务（D39）：手册承诺「cron 改了会自动生效」，此前无入口。
     D145：这个函数原先定义在 draw() **内部**（缩进 4 空格）→ 页头「新建巡检」按钮
     （定义在 render 作用域）引用不到它 → 点击直接抛 ReferenceError: editor is not defined，
     **按钮点了毫无反应**（巡检任务根本新建不了）。行内「编辑」能用是因为它也在 draw 内。
     现在提到 render 作用域，两处都拿得到。 */
  function editor(r) {
    const name = h("input", { class: "vs-input", value: r ? r.name : "每日主机巡检" });
    const cron = h("input", { class: "vs-input mono", value: r ? r.cron_expr : "0 1 * * *" });
    const enabled = h("input", { type: "checkbox", checked: (r ? r.enabled : true) ? "checked" : null });
    drawer(r ? `编辑巡检 · ${r.name}` : "新建巡检任务",
      h("div", { class: "form-row" }, h("label", {}, reqLabel("任务名")), name),
      h("div", { class: "form-row" }, h("label", {}, reqLabel("cron 表达式"), helpTip("用 **UTC** 时区（默认 UTC 01:00 = 北京 09:00）——避免各主机时区不同导致巡检跑错点。点「立即执行」可随时手动跑一次。")), cron),
      h("div", { class: "note muted" },
        "按 UTC 解释：0 1 * * * = 每天 01:00 UTC = 北京时间 09:00。保存后调度器 1 分钟内自动生效。"),
      h("div", { class: "form-row" }, h("label", {}, "启用"), enabled),
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        // D59：任务名/cron 必填先拦（服务端同口径 400）
        const bad = needText(name.value, "任务名", 128) || needText(cron.value, "cron 表达式");
        if (bad) { toast(bad, "error"); return; }
        // D64：统一 busy（转圈 + 已用秒数 + 禁用），替掉裸 disabled 标志
        try {
          const body = { name: name.value.trim(), cron_expr: cron.value.trim(),
                         target_scope: (r && r.target_scope) || { hosts: "all" }, enabled: enabled.checked };
          await busy(e.currentTarget, "保存中…", () => r
            ? api.put(`/api/ops/inspections/${r.id}`, body)
            : api.post("/api/ops/inspections", body));
          toast(r ? "已保存（调度器将自动生效）" : "已创建", "ok");
          closeDrawer(); draw();
        } catch (err) { toast(`失败：${err.message}`, "error"); }
      } }, r ? "保存" : "创建")));
  }

  async function draw() {
    let d;
    try { d = await api.get("/api/ops/inspections", { page: state.page, page_size: state.pageSize }); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    const items = d.items || [];
    body.replaceChildren(
      items.length ? table([
        { title: "序号", width: "46px", render: (_r, i) => h("span", { class: "mono muted" }, String(i + 1)) },   // D129
        { title: "任务", render: (r) => h("span", {}, r.name) },
        { title: "调度", width: "150px", render: (r) => h("span", { class: "mono muted" }, r.cron_expr + " (UTC)") },
        { title: "状态", width: "90px", render: (r) => badge(r.last_status === "ok" ? "正常" : r.last_status === "running" ? "执行中" : "未执行",
            r.last_status === "ok" ? "PASS" : r.last_status === "running" ? "RUNNING" : "SKIP") },
        { title: "最近执行", width: "120px", render: (r) => h("span", { class: "muted" }, r.last_run_at ? relTime(r.last_run_at) : "-") },
        { title: "异常项", width: "86px", render: (r) => h("span", { class: "mono" }, r.anomalies) },
        { title: "建事件", width: "86px", render: (r) => h("span", { class: "mono" }, r.incidents_created) },
        { title: "启用", width: "74px", render: (r) => badge(r.enabled ? "启用" : "停用", r.enabled ? "PASS" : "SKIP") },
        { title: "操作", width: "180px", render: (r) => h("div", { class: "row-actions" },
            h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); show(r.id); } }, "查看报告"),
            canRun ? h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); runOne(r.id, e.currentTarget); } }, "立即执行") : null,
            canWrite ? h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); editor(r); } }, "编辑") : null,
            canWrite ? h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); delOne(r, e.currentTarget); } }, "删除") : null) },
      ], items, { emptyText: "暂无巡检任务", onRowClick: (r) => show(r.id) })
        : h("div", { class: "empty" }, "暂无巡检任务"));
    pagerEl.replaceChildren(pager({ total: d.total || 0, page: state.page, pageSize: state.pageSize, unit: "个",
      onChange: (p, ps) => { state.page = p; if (ps !== state.pageSize) state.pageSize = ps; draw(); } }));

    // 新建/编辑巡检任务的编辑器已提到 render 作用域（见上），此处不再重复定义

    async function delOne(r, btn) {
      if (!window.confirm(`删除巡检任务「${r.name}」？`)) return;
      try { await busy(btn || document.activeElement, "删除中…", () => api.del(`/api/ops/inspections/${r.id}`)); toast("已删除", "ok"); draw(); }
      catch (e) { toast(`删除失败：${e.message}`, "error"); }
    }

    async function show(id) {
      const d = await api.get(`/api/ops/inspections/${id}`);
      const findings = d.findings || [];
      drawer(`巡检报告 · ${d.name}`,
        h("div", { class: "detail-meta" },
          kv("调度", h("span", { class: "mono" }, d.cron_expr + " (UTC)")),
          kv("最近执行", fmtTime(d.last_run_at)),
          kv("异常项", String(findings.length)),
          kv("生成事件", String(d.incidents_created))),
        findings.length ? h("div", { class: "card" }, h("h3", {}, "异常项"), table([
          { title: "主机", render: (r) => h("span", { class: "mono" }, r.host_name || r.host) },
          { title: "项", width: "130px", render: (r) => r.label },
          { title: "值", render: (r) => h("span", { class: "mono" }, String(r.value)) },
        ], findings)) : null,
        h("div", { class: "card" }, h("h3", {}, "报告"), h("div", { class: "md-body report-wide", html: md(d.report_md || "（尚无报告，点「立即执行」生成）") })));
    }
  }

  /* D51：巡检是长任务（跑全部纳管主机）→ 除了按钮中间态，页面顶部给一行"进行中 + 已用时间"。
     原来只有一条 3.2s 就消失的 toast，按钮还能再点（可能并发跑两次巡检）。 */
  async function runOne(id, btn) {
    const pr = progress(body, "巡检执行中（逐台主机检查 8 类项目）");
    try {
      const r = await busy(btn, "执行中…", () => api.post(`/api/ops/inspections/${id}/run`));
      const msg = `巡检完成：主机 ${r.hosts} 台 / 异常 ${r.anomalies.length} 项 / 建事件 ${r.incidents_created}`;
      pr.done(msg); toast(msg, "ok"); await draw(); show(id);
    } catch (e) { pr.fail(`巡检失败：${e.message}`); toast(`巡检失败：${e.message}`, "error"); }
  }
  async function runOnce(btn) {
    const pr = progress(body, "即时巡检执行中（不落任务表）");
    try {
      const r = await busy(btn, "执行中…", () => api.post("/api/ops/inspections/run-once"));
      const msg = `巡检完成：主机 ${r.hosts} 台 / 异常 ${r.anomalies.length} 项`;
      pr.done(msg); toast(msg, "ok"); await draw();
      // D61：不能只报个数——把异常项直接摆出来（原先用户只看到「异常 8 项」却没处看是哪 8 项）
      drawer(`即时巡检结果 · ${r.hosts} 台主机`,
        r.anomalies.length
          ? table([
              { title: "主机", render: (a) => a.host || "-" },
              { title: "异常项", render: (a) => a.item || a.metric || "-" },
              { title: "详情", render: (a) => h("span", { class: "muted" }, a.detail || a.note || "") },
            ], r.anomalies, {})
          : h("div", { class: "empty" }, "未发现异常，一切正常"));
    } catch (e) { pr.fail(`巡检失败：${e.message}`); toast(`巡检失败：${e.message}`, "error"); }
  }

  function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v)); }
  await draw();
}

export function destroy() {}
