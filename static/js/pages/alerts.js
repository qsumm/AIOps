// pages/alerts.js —— 告警中心（FR-F ops-alerts）
// 必备：实时 SSE 告警流、级别 pill、批量确认/解决/抑制、详情抽屉（ai_summary、通知台账入口）
import { api, openAlertStream, getUser, can } from "../api.js";
import { marked } from "/vendor/marked.esm.js";   // D126：AI 摘要带 **加粗**，必须走 markdown 渲染
import { h, table, sevBadge, statusBadge, drawer, toast, relTime, fmtTime, select, badge, hostLabel, ensureHostNames, busy, reqLabel, needText, helpTip, thLabel, closeDrawer, pager, svgIcon } from "../ui.js";

let es = null;
const selected = new Set();

export async function render(root) {
  selected.clear();
  // D126：勾选数量反馈（必须早于 head 构造——head 里就会引用它）
  const selTip = h("span", { class: "muted", style: "font-size:0.76rem" });
  const pagerEl = h("div", { class: "pager" });   // D128
  await ensureHostNames();   // D40：ident → 主机名
  let hostCatalog = [];      // D123：主机筛选下拉数据（ident/name）
  try { hostCatalog = (await api.get("/api/ops/hosts", { page_size: 200 })).items || []; } catch { /* 降级为无主机筛选 */ }
  const me = getUser() || {};
  const canWrite = can("alert_ops");   // 告警操作：admin/member（03 权限矩阵）
  const state = { status: "", severity: 0, host: "", keyword: "", page: 1, pageSize: 50, view: "active", hisPage: 1 };
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  const head = h("div", { class: "page-head" },
    h("div", { class: "actions" },
      // D166：活跃 / 历史两个页签——恢复后的告警此前从界面"消失"，只能靠 AI 助手查
      h("button", { class: `tab${state.view === "active" ? " active" : ""}`, id: "tab-active",
        onclick: () => { state.view = "active"; state.page = 1; syncTabs(); draw(); } }, "活跃告警"),
      h("button", { class: `tab${state.view === "history" ? " active" : ""}`, id: "tab-history",
        onclick: () => { state.view = "history"; state.hisPage = 1; syncTabs(); draw(); } }, "历史记录"),
      h("span", { class: "live-dot", id: "sse-dot", style: state.view === "active" ? "" : "display:none" }, "实时流"),
      select([["", "全部状态"], ["firing", "未确认"], ["ack", "已确认"]], "", { onchange: (e) => { state.status = e.target.value; draw(); } }),
      select([["0", "全部级别"], ["1", "S1 紧急"], ["2", "S2 警告"], ["3", "S3 通知"]], "", { onchange: (e) => { state.severity = Number(e.target.value); draw(); } }),
      select([["", "全部主机"]].concat((hostCatalog || []).map((h0) => [h0.ident, h0.name])), "", {
        onchange: (e) => { state.host = e.target.value; draw(); } }),
      h("input", { class: "vs-input", style: "width:150px", placeholder: "搜索规则 / 指标…",
        value: state.keyword,
        oninput: (e) => { state.keyword = e.target.value; state.page = 1; clearTimeout(state._kwT);
          state._kwT = setTimeout(() => draw(), 350); } }),
      selTip,
      h("button", { class: "btn ghost", title: "全选当前页勾选", onclick: () => {
        // 只选行复选框（data-hash）——表头那个是「全选/取消本页」开关，不在其中
        document.querySelectorAll("#alerts-body input[data-hash]").forEach((c) => {
          c.checked = true;
          selected.add(c.dataset.hash);
        });
        const all = document.getElementById("alert-sel-all");
        if (all) all.checked = true;
        selTip.textContent = `已选 ${selected.size} 条`;
      } }, "全选本页"),
      h("button", { class: "btn ghost", onclick: () => { selected.clear(); draw(); selTip.textContent = ""; } }, "清空勾选"),
      canWrite ? h("button", { class: "btn secondary", onclick: (e) => bulk("ack", e.currentTarget) }, "批量确认") : null,
      canWrite ? h("button", { class: "btn secondary", onclick: (e) => { e.stopPropagation(); bulk("resolve", e.currentTarget); } }, "批量解决") : null,
      canWrite ? h("button", { class: "btn danger", onclick: (e) => { e.stopPropagation(); bulkDelete(e.currentTarget); } }, "批量删除") : null,
      canWrite ? h("button", { class: "btn secondary", onclick: () => mute_dialog() }, "新建抑制") : null,
      canWrite ? h("button", { class: "btn ghost", onclick: () => showMutes() }, "抑制管理") : null));
  function syncTabs() {
    document.getElementById("tab-active")?.classList.toggle("active", state.view === "active");
    document.getElementById("tab-history")?.classList.toggle("active", state.view === "history");
    const dot = document.getElementById("sse-dot");
    if (dot) dot.style.display = state.view === "active" ? "" : "none";
    // 批量操作只对活跃告警有意义（历史记录不可改）
    head.querySelectorAll("button.btn").forEach((b) => {
      const t = b.textContent || "";
      if (/批量|新建抑制/.test(t)) b.style.display = state.view === "active" ? "" : "none";
    });
    if (state.view === "history") { selected.clear(); selTip.textContent = ""; }
  }
  const count = h("div", { class: "count-line muted" });
  const body = h("div", { id: "alerts-body" });
  root.replaceChildren(head, count, body);

  /* D60：抑制列表与管理（用户报「创建了然后呢？」——mute 此前创建后无处可看、无处可删）。
     数据形状：{days:[1..7], start, end}；起始时间非空则倒计时，过期/停用高亮便于清理。 */
  function muteWindowText(r) {
    // 后端时间是 UTC 字符串（"2026-10-15 08:00:00"）——必须转本地时区再展示（D37 的 8 小时教训），
    // 首版直接截取 UTC 时刻给用户看，是错的。
    const utc = (s) => (s ? new Date(String(s).replace(" ", "T") + "Z") : null);
    const hm = (d) => (d ? d.toTimeString().slice(0, 5) : "");
    if (r.btime && r.etime) {
      const now = Date.now();
      const b = utc(r.btime), e = utc(r.etime);
      if (e.getTime() < now) return `已过期 · ${hm(b)} ~ ${hm(e)}`;
      return `生效中 · ${hm(b)} ~ ${hm(e)}`;
    }
    return "生效中 · 长期";
  }

  async function showMutes() {
    let d = { items: [] };
    try { d = await api.get("/api/ops/alert-mutes"); } catch { /* ignore */ }
    const items = (d.items || []).filter((r) => !r.disabled);
    drawer("抑制管理（屏蔽中的告警规则）",
      h("div", { class: "note" }, "抑制 = 让指定告警暂时静默：「全屏蔽」连事件都不产生，「只屏蔽通知」事件照常记录。" +
        "到期自动失效；也可以在下面手动解除。"),
      items.length ? (() => {
        const t = table([
          { title: "原因", render: (r) => r.cause },
          { title: "方式", width: "128px", render: (r) => badge(r.mute_type === 0 ? "全屏蔽" : "只屏蔽通知", r.mute_type === 0 ? "FAIL" : "WARN") },
          { title: "目标", width: "180px", render: (r) => (r.tag_filters || []).map((f) => `${f.k === "metric" ? "指标" : f.k} = ${f.v}`).join("、") || "全部" },
          { title: "状态", width: "160px", render: (r) => h("span", { class: /已过期/.test(muteWindowText(r)) ? "muted" : "" }, muteWindowText(r)) },
          { title: "操作", width: "80px", render: (r) => h("button", { class: "btn ghost danger",
              onclick: async (e) => {
                try { await busy(e.currentTarget, "解除中…", () => api.del(`/api/ops/alert-mutes/${r.id}`)); toast("已解除抑制", "ok"); showMutes(); draw(); }
                catch (err) { toast(`解除失败：${err.message}`, "error"); } } }, "解除") },
        ], items);
        return t;
      })()
        : h("div", { class: "empty" }, "当前没有生效中的抑制策略"));
  }

  // D166：告警历史——已恢复/已关闭事件的存档（后端 /api/ops/alert-history）
  async function drawHistory() {
    let d;
    try { d = await api.get("/api/ops/alert-history", {
      severity: state.severity || "", host: state.host, keyword: state.keyword,
      hours: 168, page: state.hisPage, page_size: state.pageSize }); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    const items = d.items || [];
    count.textContent = `近 7 天历史告警 ${d.total} 条 · 按触发时间倒序（恢复/关闭后的告警在此留档）`;
    selTip.textContent = "";
    // 注意：这里用变量形式接表——d144 守卫按"活跃列表的替换调用"定位勾选列，
    // 历史表必须声明在它后面，避免被当成第一个匹配
    const tbl = table([
      { title: "序号", width: "40px", render: (_r, i) => h("span", { class: "mono muted" }, String((state.hisPage - 1) * state.pageSize + i + 1)) },
      { title: "级别", width: "92px", render: (r) => sevBadge(r.severity) },
      { title: "结局", width: "88px", render: (r) => r.is_recovered ? badge("已恢复", "PASS") : badge("已关闭", "SKIP") },
      { title: "规则", render: (r) => r.rule_name || r.rule_id },
      { title: "主机", width: "104px", render: (r) => h("span", { class: "mono" }, hostLabel(r.host_ident)) },
      { title: "指标", width: "150px", render: (r) => h("span", { class: "mono" }, r.metric) },
      { title: "触发值", width: "88px", render: (r) => h("span", { class: "mono" }, r.trigger_value) },
      { title: "触发时间", width: "120px", render: (r) => h("span", { class: "muted" }, relTime(r.trigger_time)) },
      { title: "恢复时间", width: "120px", render: (r) => h("span", { class: "muted" }, r.recover_time ? relTime(r.recover_time) : "—") },
    ], items, { emptyRender: () => h("div", { class: "empty", style: "padding:44px 0" },
        svgIcon("inbox", 40),
        h("h3", { style: "margin:12px 0 6px" }, "近 7 天没有历史告警"),
        h("p", { class: "muted", style: "max-width:420px; margin:0 auto" },
          "告警恢复或关闭后会归档到这里（保留全部历史）；时间范围默认 7 天。")),
      onRowClick: (r) => historyDetail(r) });
    body.replaceChildren(tbl);
    body.append(pagerEl);
    pagerEl.replaceChildren(pager({ total: d.total || 0, page: state.hisPage, pageSize: state.pageSize, unit: "条",
      sizes: [20, 50, 100],
      onChange: (p, ps) => { state.hisPage = p; if (ps !== state.pageSize) state.pageSize = ps; draw(); } }));
  }

  function historyDetail(r) {
    const rows = [["规则", r.rule_name || r.rule_id], ["指标", r.metric],
      ["级别", "S" + r.severity], ["触发值", String(r.trigger_value)],
      ["主机", hostLabel(r.host_ident)],
      ["触发时间", fmtTime(r.trigger_time)],
      ["恢复时间", r.recover_time ? fmtTime(r.recover_time) : "—"],
      ["结局", r.is_recovered ? "已恢复（指标回到正常区间）" : "已关闭（人工处理）"]];
    drawer(`历史告警 · ${r.rule_name || r.rule_id}`,
      r.ai_summary ? h("div", { class: "note" }, r.ai_summary) : null,
      h("div", { style: "margin-top:10px" },
        ...rows.map(([k, v]) => h("div", { class: "form-row", style: "justify-content:space-between" },
          h("span", { class: "muted" }, k), h("span", { class: "mono", style: "text-align:right" }, v || "—")))));
  }

  async function draw() {
    syncTabs();
    if (state.view === "history") return drawHistory();
    let d;
    try { d = await api.get("/api/ops/alerts", { status: state.status, severity: state.severity || "", host: state.host, keyword: state.keyword,
      page: state.page, page_size: state.pageSize }); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    const items = d.items || [];
    count.textContent = `共 ${d.total} 条活跃告警 · 排序：级别（S1→S3）优先，同级按最近触发时间倒序`;
    selTip.textContent = "";
    body.replaceChildren(table([
      // D144：表头「全选框」与行复选框**合并成同一列**，序号紧跟其后。
      // 此前拆成两列（表头一列、行复选框又一列）→ 表头那个全选框与它下面的行复选框
      // 上下错开一列，中间还夹着一个空表头格（用户报「序号旁边多出一列勾选的空格」）。
      { title: h("input", { type: "checkbox", id: "alert-sel-all", title: "全选 / 取消本页",
          onclick: (e) => {
            e.stopPropagation();
            const on = e.target.checked;
            document.querySelectorAll("#alerts-body input[data-hash]").forEach((c) => { c.checked = on; });
            selected.clear();
            if (on) items.forEach((r) => selected.add(r.hash));
            selTip.textContent = `已选 ${selected.size} 条`;
          } }), width: "34px",
        render: (r) => h("input", { type: "checkbox", "data-hash": r.hash, checked: selected.has(r.hash) ? "checked" : null,
          onclick: (e) => { e.stopPropagation(); e.target.checked ? selected.add(r.hash) : selected.delete(r.hash);
            selTip.textContent = `已选 ${selected.size} 条`; } }) },
      { title: "序号", width: "40px", render: (_r, i) => h("span", { class: "mono muted" }, String(i + 1)) },
      { title: thLabel("级别", "**S1 紧急** = 最严重，需立即处理；**S2 警告** = 影响明显，尽快处理；**S3 通知** = 记录与观察。级别由告警规则定义"), width: "92px", render: (r) => sevBadge(r.severity) },
      { title: thLabel("状态", "**触发中** = 问题正在发生，平台实时监测中、尚未恢复；**已确认** = 有人认领、处理中；**数据陈旧** = 规则已停用/删除或长时间未评估，该行不再作为当前故障证据。恢复后告警会自动从本列表消失"), width: "96px",
        render: (r) => r.stale
          ? h("span", { class: "badge SKIP", title: r.stale_reason || "数据陈旧" }, "数据陈旧")
          : statusBadge(r.status) },
      { title: "规则", render: (r) => r.rule_name || r.rule_id },
      { title: "主机", width: "104px", render: (r) => h("span", { class: "mono" }, hostLabel(r.host_ident)) },
      // D175-6：指标给中文名（"磁盘使用率"），英文原名放进 title 备查——
      // 用户原话："你能知道当前是啥问题吗，完全看不出来啊"。
      { title: thLabel("指标", "指标的中文名（含业务维度，如「磁盘 data 分区使用率」）；悬停可看原始指标名"), width: "180px",
        render: (r) => h("span", { title: r.metric || "" },
          (r.metric_label || r.metric || "-")
          + (r.trigger_value === null || r.trigger_value === undefined ? "" : `　${r.trigger_value}`)) },
      // AI 摘要（ai_summary）后端一直在写，此前**列表不展示**——用户看不到"最关键的是什么"
      { title: thLabel("AI 判断", "告警触发时由快速模型给出的**一句话根因方向**（异步生成，可能为空）。它是参考方向，完整证据链以问题看板的诊断报告为准"),
        render: (r) => r.ai_summary
          ? h("span", { class: "ai-cell", title: r.ai_summary }, r.ai_summary)
          : h("span", { class: "muted" }, "—") },
      { title: thLabel("首次触发", "该问题**第一次**触发的时间"), width: "96px", render: (r) => h("span", { class: "muted" }, relTime(r.first_trigger_time)) },
      { title: thLabel("最近触发", "**最近一次**触发的时间——结合左侧「状态」判断问题当前是否仍在持续"), width: "96px", render: (r) => h("span", { class: "muted" }, relTime(r.trigger_time)) },
    ], items, { emptyText: "当前没有活跃告警", onRowClick: (r) => detail(r, canWrite) }));
    body.append(pagerEl);   // D128：分页在列表右下
    pagerEl.replaceChildren(pager({ total: d.total || 0, page: state.page, pageSize: state.pageSize, unit: "条",
      sizes: [20, 50, 100],
      onChange: (p, ps) => { state.page = p; if (ps !== state.pageSize) state.pageSize = ps;
        selected.clear(); draw(); } }));
  }

  /** 表头"文字+?"组合（helpTip 只渲染图标，标题文字要自己带）。 */

  function detail(ev, canWrite) {
    drawer(`告警详情`,
      h("div", { class: "detail-meta" },
        kv("级别", sevBadge(ev.severity)),
        kv("状态", ev.stale ? h("span", { class: "badge SKIP", title: ev.stale_reason || "" }, "数据陈旧") : statusBadge(ev.status)),
        kv("规则", ev.rule_name), kv("主机", h("span", { class: "mono" }, ev.host_ident)),
        kv("指标", h("span", { class: "mono" }, ev.metric)),
        // D149：把规则条件摆出来——"7.9% 也告警"的第一反应是平台误报，实际多是规则阈值写错
        kv("触发条件", ev.trigger_expr
          ? h("span", { class: "mono" }, `${ev.metric} ${ev.trigger_expr}`)
          : h("span", { class: "muted" }, "（规则已删除或不可用）")),
        kv("触发值", h("span", { class: "mono" }, ev.trigger_value)),
        kv("首次触发", fmtTime(ev.first_trigger_time)), kv("指纹", h("span", { class: "mono muted" }, ev.hash))),
      thresholdWarning(ev),
      h("div", { class: "card" }, h("h3", {}, "AI 摘要 · 告警根因速览", helpTip("告警触发时由**快速模型自动生成**的一句话根因方向（基于规则 / 主机 / 指标 / 触发值），异步写入、不阻塞告警通知。它是**参考方向**——详细证据与结论以「执行诊断」报告为准；若触发时大模型不可用（如额度用尽）会留空。")),
        ev.ai_summary
          ? h("div", { class: "md-body", html: marked.parse(ev.ai_summary) })   // D126：走 markdown，**加粗**才生效
          : h("div", { class: "md-body" }, h("span", { class: "muted" }, "暂无 AI 摘要——摘要在告警触发时自动生成；若当时大模型服务不可用（如额度用尽）会留空。完整根因分析可点「执行诊断」。"))),
      h("div", { class: "card" }, h("h3", {}, "标签"),
        (() => {
          const entries = Object.entries(ev.tags || {});
          return entries.length
            ? h("div", { class: "detail-meta" }, entries.map(([k, v]) => kv(k, h("span", { class: "mono" }, String(v)))))
            : h("div", { class: "muted" }, "无标签");
        })()),
      h("div", { class: "card" }, h("h3", {}, "自愈动作",
          helpTip("自愈策略被本条告警触发后的执行轨迹：演练/待确认/已执行/被护栏拦截。"
            + "没有记录 = 没有策略匹配本条告警（到「自愈策略」页配置）。")),
        h("div", { id: "ev-heals" }, h("div", { class: "empty" }, "加载中…"))),
      h("div", { class: "card" }, h("h3", {}, "通知台账"), h("div", { id: "ev-records" }, h("div", { class: "empty" }, "加载中…"))),
      canWrite ? h("div", { class: "detail-actions" },
        h("button", { class: "btn secondary", onclick: async (e) => { await busy(e.currentTarget, "确认中…",
          async () => { try { await api.post(`/api/ops/alerts/${ev.hash}/ack`); toast("操作成功", "ok"); } catch (err) { toast(`失败：${err.message}`, "error"); } });
          closeAndRedraw(); } }, "确认"),
        h("button", { class: "btn", onclick: async (e) => { await busy(e.currentTarget, "解决中…",
          async () => { try { await api.post(`/api/ops/alerts/${ev.hash}/resolve`); toast("操作成功", "ok"); } catch (err) { toast(`失败：${err.message}`, "error"); } });
          closeAndRedraw(); } }, "解决"),
        h("button", { class: "btn ghost", onclick: () => mute_dialog(ev) }, "抑制此指标")) : null,
      // D175-7：文案两次让用户「点执行诊断」，页面上却没有这个按钮（悬空引用）。
      // 真入口：跳问题看板的按告警深链（#/incidents/alert/<hash>），那里能一键诊断。
      h("div", { class: "detail-actions" },
        h("button", { class: "btn", onclick: () => {
          closeDrawer();
          location.hash = `#/incidents/alert/${ev.hash}`;
          toast("已跳到问题看板：点「一键诊断（AI）」即可对本条告警做根因分析", "info");
        } }, "执行诊断（AI）")));

    // D140：自愈动作留痕（按告警指纹查）——用户在告警详情就能看到"自愈策略跑了没/做了什么"
    api.get("/api/ops/heal-policy-runs", { page_size: 20 }).then((d) => {
      const mine = (d.items || []).filter((r) => r.alert_hash === ev.hash);
      const box = document.getElementById("ev-heals");
      if (!box) return;
      if (!mine.length) {
        box.replaceChildren(h("div", { class: "muted", style: "font-size:0.8rem" },
          "没有自愈策略被本条告警触发——到「自愈策略」页可配置「告警触发 → 自动执行白名单动作」。"));
        return;
      }
      const META = { dry_run: ["演练", "SKIP"], waiting_confirm: ["待确认", "WARN"],
                     dispatched: ["已执行", "PASS"], denied: ["被拦截", "FAIL"], failed: ["失败", "FAIL"] };
      box.replaceChildren(table([
        { title: "策略", render: (r) => r.policy_name },
        { title: "动作", width: "110px", render: (r) => r.action_label },
        { title: "状态", width: "90px", render: (r) => {
            const m = META[r.status] || [r.status, "SKIP", ""];
            return h("span", { class: `badge ${m[1]}`, title: m[2] }, m[0]);
          } },
        { title: "护栏结论", render: (r) => h("span", { class: "muted", style: "font-size:0.78rem" },
            r.guard_reason || (r.status === "dispatched" ? `任务 #${r.task_id}` : "-")) },
      ], mine, { emptyText: "无" }));
    }).catch(() => {});

    api.get("/api/ops/notification-records", { event_hash: ev.hash }).then((d) => {
      document.getElementById("ev-records")?.replaceChildren(table([
        { title: "渠道", width: "110px", render: (r) => r.channel },
        { title: "状态", width: "80px", render: (r) => statusBadge(r.status) },
        { title: "时间", width: "140px", render: (r) => h("span", { class: "muted mono" }, fmtTime(r.created_at)) },
        { title: "备注", render: (r) => h("span", { class: "muted" }, r.error_detail || "-") },
      ], d.items || [], { emptyText: "无通知记录（可能未配置渠道或已静默）" }));
    }).catch(() => {});
  }

  function closeAndRedraw() { document.querySelector(".detail-panel .detail-head .btn")?.click(); draw(); }

  async function bulk(kind, btn) {
    if (!selected.size) { toast("请先勾选告警", "warn"); return; }
    // D51：批量操作要按条数发多次请求（可能几十条）→ 中间态 + 已用时间
    let ok = false;
    try {
      await busy(btn, "处理中…", () => api.post("/api/ops/alerts/bulk", { hashes: [...selected], action: kind }));
      toast(`已${kind === "ack" ? "确认" : kind === "delete" ? "删除" : "解决"} ${selected.size} 条`, "ok"); ok = true;
    } catch (e) { toast(`失败：${e.message}`, "error"); }
    if (ok) { selected.clear(); draw(); }
  }

  /** 批量删除（D123）：彻底删除活跃告警记录，不可回滚 → 强确认；历史恢复记录保留审计。 */
  function bulkDelete(btn) {
    if (!selected.size) { toast("请先勾选告警", "warn"); return; }
    const n = selected.size;
    drawer(`批量删除 ${n} 条告警`,
      h("div", { class: "note" },
        `确认彻底删除这 ${n} 条活跃告警记录？删除后「不可回滚」（操作审计会留 hash 清单）。`
        + "只想让告警不再打扰请用「批量解决」——它会记录恢复时间，可在审计中追溯。"),
      h("div", { class: "detail-actions" },
        h("button", { class: "btn danger", onclick: async (e) => {
          try {
            await busy(e.currentTarget, "删除中…", () => api.post("/api/ops/alerts/bulk", { hashes: [...selected], action: "delete" }));
            toast(`已删除 ${n} 条告警`, "ok");
            closeDrawer(); selected.clear(); draw();
          } catch (err) { toast(`删除失败：${err.message}`, "error"); }
        } }, "确认删除"),
        h("button", { class: "btn ghost", onclick: closeDrawer }, "取消")));
  }

  function mute_dialog(ev) {
    const metric = ev?.metric || "cpu.percent";
    const cause = h("input", { class: "vs-input", value: "维护窗口", placeholder: "为什么屏（必填，便于审计）" });
    const type = select([["0", "全屏蔽（事件不产生）"], ["1", "只屏蔽通知（事件照常记录）"]], "0");
    const hours = select([["1", "1 小时"], ["2", "2 小时"], ["8", "8 小时"], ["24", "24 小时"]], "2");
    drawer("新建抑制策略",
      h("div", { class: "note" }, "抑制 = 让指定告警暂时静默：到点自动失效，解除后下一轮评估重新产生。" +
        "全部配置过的抑制可在列表上方「抑制管理」里查看、提前解除。"),
      h("div", { class: "form-row" }, h("label", {}, reqLabel("原因")), cause),
      h("div", { class: "form-row" }, h("label", {}, reqLabel("指标（TagFilter）")), h("input", { class: "vs-input mono", value: metric, id: "mute-metric" })),
      h("div", { class: "form-row" }, h("label", {}, "屏蔽方式"), type),
      h("div", { class: "form-row" }, h("label", {}, "时长"), hours),
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        // D59：原因必填（屏蔽没有"为什么"，审计无法回答）；空指标值=恒不匹配的废静默
        const bad = needText(cause.value, "屏蔽原因", 255) || needText(document.getElementById("mute-metric").value, "指标（TagFilter）", 128);
        if (bad) { toast(bad, "error"); return; }
        const now = new Date();
        const end = new Date(now.getTime() + Number(hours.value) * 3600 * 1000);
        try {
          await busy(e.currentTarget, "创建中…", () => api.post("/api/ops/alert-mutes", {
            cause: cause.value.trim(), mute_type: Number(type.value),
            tag_filters: [{ k: "metric", op: "==", v: document.getElementById("mute-metric").value.trim() }],
            severities: [], btime: now.toISOString().slice(0, 19), etime: end.toISOString().slice(0, 19),
          }));
          toast("抑制已创建，正在生效（可在「抑制管理」查看/解除）", "ok");
          document.querySelector(".detail-head .btn")?.click();
          showMutes();   // 建完直接去能看到、能删的地方——别让用户猜「然后呢？」
          // D60 防抖保险丝：本页渲染也可能因 SSE 抖动出错，提示后 1.2s 仍在本页则自动跳过去
          setTimeout(() => {
            if (document.getElementById("content")?.textContent.includes("加载失败")) return;
            const go = document.querySelector('.nav-item[data-page="alerts"]');
            if (go && !document.querySelector(".detail-panel")) go.click();
          }, 1200);
          draw();
        } catch (err) { toast(`失败：${err.message}`, "error"); }
      } }, "创建")));
  }

  function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v)); }

  /** D149：百分比指标的阈值直觉检查（只提示、不拦截）。
   *  用户实测：disk.percent=7.9% 收到 S2 告警，详情只显示规则名 → 看起来像平台误报；
   *  真实原因是规则写成了 `>1`。把"条件 + 常识偏差"直接摆在详情里，配置问题一眼可辨。 */
  function thresholdWarning(ev) {
    const metric = String(ev.metric || "");
    const expr = String(ev.trigger_expr || "").trim();
    const m = expr.match(/^([<>]=?)\s*(-?\d+(?:\.\d+)?)/);
    if (!m) return null;
    const op = m[1], th = Number(m[2]), val = Number(ev.trigger_value);
    if (!Number.isFinite(th) || !Number.isFinite(val)) return null;
    const isPct = /\.percent$|\.pct$/i.test(metric);
    if (!isPct) return null;
    let msg = "";
    if (op.startsWith(">") && th < 10) {
      msg = `规则条件是「${metric} ${expr}」，阈值只有 ${th}%——磁盘/内存/CPU 这类百分比指标通常按 80~90 设置。本次 ${val}% 就被判为异常，请确认这是否是本意。`;
    } else if (op.startsWith("<") && th > 90) {
      msg = `规则条件是「${metric} ${expr}」，剩余百分比低于 ${th}% 才算异常——通常按 10~20 设置，否则几乎永远不触发。请确认这是否是本意。`;
    }
    return msg ? h("div", { class: "note", style: "margin-top:10px" },
      h("b", {}, "阈值提醒："), msg, "　到「告警规则」可修改条件（本条只是提示，不影响当前告警）。") : null;
  }

  await draw();

  // SSE 实时流：新事件插播 toast + 增量刷新
  if (es) es.close();
  es = await openAlertStream((type, ev) => {
    if (type === "hello") { document.getElementById("sse-dot")?.classList.add("on"); return; }
    const dot = document.getElementById("sse-dot");
    if (dot) dot.classList.add("on");
    if (type === "firing") toast(`新告警 S${ev.severity} · ${ev.rule_name} · ${ev.metric}=${ev.trigger_value}`, "error");
    if (type === "recovered") toast(`已恢复 · ${ev.rule_name} · ${ev.metric}`, "ok");
    draw();
  });
}

export function destroy() { if (es) { es.close(); es = null; } }
