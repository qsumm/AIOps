// pages/cases.js —— 案例库（FR-416）
//
// 为什么要有这一页：事件关闭时平台会自动沉淀**案例卡**（症状/根因/处置），诊断时用向量召回
// 让 AI 说"上次同类故障是这么解的"。此前案例卡只有 API、**界面上看不到也改不了**，后果是：
//   · 客户无法确认"平台到底学到了什么"（只能某次诊断里偶然看到它引用了一条）；
//   · **错了没法纠正**——某次根因判断偏差、或那次处置其实无效，这张卡会一直参与召回、
//     持续污染后续诊断（D69 实测：一张短泛化卡就把 Top-1 命中率从 78% 拉到 67%）。
// 所以这一页提供的核心能力是：**看得到、找得到、改得动、能停用/删除**，外加手工沉淀。
import { api, can } from "../api.js";
import { marked } from "/vendor/marked.esm.js";
import { h, table, badge, drawer, toast, select, note, busy, fmtTime, fill, helpTip, needText, kpiRow, pager } from "../ui.js";

const md = (text) => {
  try { return String(marked.parse(text || "", { breaks: true, gfm: true })); }
  catch { return `<pre>${String(text || "").replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]))}</pre>`; }
};

/** tags 容错解析：接口现在保证返回数组，但历史上返回过字符串
 *  （raw SQL 查 MySQL JSON 列 → 驱动给的是 `'["a","b"]'`）——直接用 `.join` 会抛异常，
 *  表现就是"点「查看 / 编辑」没反应"（用户实测报障）。这里两种形态都吃。
 */
function tagsArr(v) {
  if (Array.isArray(v)) return v;
  if (typeof v === "string") {
    try { const p = JSON.parse(v || "[]"); return Array.isArray(p) ? p : []; } catch { return []; }
  }
  return [];
}

export async function render(root) {
  const canWrite = can("write");
  const state = { q: "", enabled: "" , page: 1, pageSize: 20 };
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  const qBox = h("input", { class: "vs-input", placeholder: "搜索症状 / 根因 / 处置…", style: "width:240px",
    oninput: (e) => { state.q = e.target.value; state.page = 1; clearTimeout(qBox._t); qBox._t = setTimeout(draw, 350); } });
  const head = h("div", { class: "page-head" },
    h("div", { class: "actions" },
      qBox,
      select([["", "全部状态"], ["1", "参与检索"], ["0", "已停用"]], "", {
        onchange: (e) => { state.enabled = e.target.value; state.page = 1; draw(); } }),
      h("button", { class: "btn secondary", onclick: draw }, "刷新"),
      canWrite ? h("button", { class: "btn", onclick: () => editor(null) }, "新建案例卡") : null));
  const stats = h("div", { class: "svc-summary" });
  const tip = note("**案例库 = 平台的经验**。事件关闭时会自动沉淀一张卡（症状 / 根因 / 处置），"
    + "诊断时按语义召回，让 AI 参考「上次同类故障是怎么解的」。"
    + "**参与检索**列表示这张卡是否有向量（没有向量的卡只能被关键词搜到，不参与语义召回）——"
    + "文本太短的卡会被刻意跳过向量（泛化文本会把不相关的查询也吸过来）。"
    + "如果某张卡判断有偏差或处置无效，**在这里改掉或停用它**：错卡留着会持续污染后续诊断。");
  const body = h("div", { id: "case-body" });
  const pagerEl = h("div", { class: "pager" });   // D128
  root.replaceChildren(head, tip, stats, body, pagerEl)

  /** 顶部两块计数：都放**数**（"案例卡总数"与"能参与语义检索的张数"）。
   *
   *  为什么不放"向量检索：已启用"：状态词塞进数字卡里要么被放大成巨型绿字、要么塞个徽章显得别扭
   *  （用户实测："太大了，还有点丑，精致一点"）。状态异常时才在下面给一行提示——正常态不必占位。
   */
  async function drawStats(total, embedCount) {
    let st = null;
    try { st = await api.get("/api/ops/case-cards/stats"); } catch { /* ignore */ }
    if (!st) { stats.replaceChildren(); return; }
    fill(stats,
      kpiRow([
        { label: "案例卡", value: total, cls: "" },
        { label: "可语义检索", value: embedCount, cls: embedCount ? "" : "skip" },
      ]),
      st.vector_enabled ? null : h("div", { style: "margin-top:10px" }, note(
        "向量检索当前**已关闭**（LLM 的 Embedding 不可用或未配置）：案例只能按**关键词**搜到，"
        + "不会参与语义召回。配置好 Embedding 模型后会自动恢复。")));
  }

  async function draw() {
    let d = null;
    try { d = await api.get("/api/ops/case-cards", { q: state.q, enabled: state.enabled, page: state.page, page_size: state.pageSize }); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    const items = d.items || [];
    const embedCount = items.filter((x) => x.has_embedding).length;
    if (!items.length) {
      drawStats(d.total, 0);
      body.replaceChildren(h("div", { class: "empty" }, state.q || state.enabled
        ? "没有匹配的案例卡——换个关键词，或把状态筛选切回「全部状态」"
        : "案例库还是空的：关闭一次故障事件就会自动沉淀一张卡；也可以点右上「新建案例卡」手工加一条（例如厂商已知问题）"));
      return;
    }
    drawStats(d.total, embedCount);
    pagerEl.replaceChildren(pager({ total: d.total || 0, page: state.page, pageSize: state.pageSize, unit: "张",
      onChange: (p, ps) => { state.page = p; if (ps !== state.pageSize) state.pageSize = ps; draw(); } }));
    body.replaceChildren(h("div", { class: "muted", style: "font-size:0.78rem;margin-bottom:6px" },
      `共 ${d.total} 张（按创建时间倒序）`),
      table([
        { title: "序号", width: "46px", render: (_r, i) => h("span", { class: "mono muted" }, String(i + 1)) },   // D129
        { title: "症状", render: (r) => h("span", {}, (r.symptom || "").replace(/\*\*/g, "").replace(/\n+/g, " ")) },
        { title: ["根因", helpTip("这张卡记录的判定原因。发现判断有偏差就在这里改掉——错卡会持续污染后续召回。")], render: (r) => h("span", { class: "muted" }, (r.root_cause || "").replace(/\*\*/g, "").replace(/\n+/g, " ").slice(0, 60)) },
        { title: "来源", width: "96px", render: (r) => r.incident_id
            ? h("span", { class: "mono muted" }, `事件 #${r.incident_id}`) : badge("手工", "ACCENT") },
        { title: ["参与检索", helpTip("有向量 = 能被**语义**召回（换个说法也能找到它）；无向量 = 只能被关键词搜到。文本过短的卡会被刻意跳过向量，避免泛化文本污染检索。")], width: "96px",
          render: (r) => badge(r.has_embedding ? "语义" : "仅关键词", r.has_embedding ? "PASS" : "SKIP") },
        { title: "状态", width: "80px", render: (r) => badge(r.enabled ? "启用" : "停用", r.enabled ? "PASS" : "SKIP") },
        { title: "创建", width: "140px", render: (r) => h("span", { class: "muted mono", style: "font-size:0.76rem" }, fmtTime(r.created_at)) },
        { title: "操作", width: "230px", render: (r) => h("div", { class: "row-actions" },
            h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); detail(r); } }, "查看"),
            canWrite ? h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); editor(r); } }, "编辑") : null,
            canWrite ? h("button", { class: "btn ghost", onclick: async (e) => {
              e.stopPropagation();
              try { await busy(e.currentTarget, "处理中…", () => api.patch(`/api/ops/case-cards/${r.id}`, { enabled: !r.enabled }));
                toast(r.enabled ? "已停用（不再参与诊断召回）" : "已启用", "ok"); draw(); }
              catch (err) { toast(`操作失败：${err.message}`, "error"); }
            } }, r.enabled ? "停用" : "启用") : null,
            canWrite ? h("button", { class: "btn ghost danger", onclick: async (e) => {
              e.stopPropagation();
              if (!window.confirm(`删除这张案例卡？\n\n症状：${r.symptom}\n\n删除后诊断不会再参考它。`)) return;
              try { await busy(e.currentTarget, "删除中…", () => api.del(`/api/ops/case-cards/${r.id}`));
                toast("已删除", "ok"); draw(); }
              catch (err) { toast(`删除失败：${err.message}`, "error"); }
            } }, "删除") : null) },
      ], items, {}));
  }

  function detail(r) {
    drawer(`案例卡 #${r.id}`,
      note(r.incident_id ? `来自事件 #${r.incident_id} 的自动沉淀（点下方可跳到事件）` : "手工录入的案例"),
      h("div", { class: "detail-meta" },
        kv("状态", badge(r.enabled ? "启用" : "停用", r.enabled ? "PASS" : "SKIP")),
        kv("参与检索", badge(r.has_embedding ? "语义召回 + 关键词" : "仅关键词", r.has_embedding ? "PASS" : "SKIP")),
        kv("创建时间", fmtTime(r.created_at))),
      h("div", { class: "card" }, h("h3", {}, "症状"), h("div", { class: "md-body", html: md(r.symptom || "") })),
      h("div", { class: "card" }, h("h3", {}, "根因"), h("div", { class: "md-body", html: md(r.root_cause || "") })),
      (r.resolution_md || "").trim()
        ? h("div", { class: "card" }, h("h3", {}, "处置 / 解决方案"), h("div", { class: "md-body cb-tall", html: md(r.resolution_md) }))
        : h("div", { class: "note" }, "这张卡没有记录处置过程——下次诊断时它只能提示「症状与根因」，给不出解法步骤。编辑补上会明显提升参考价值。"),
      tagsArr(r.tags).length ? h("div", { class: "muted", style: "font-size:0.78rem" }, "标签：" + tagsArr(r.tags).join(" / ")) : null,
      r.incident_id ? h("div", { class: "detail-actions" },
        h("button", { class: "btn secondary", onclick: () => { location.hash = `#/incidents`; toast("已切到问题看板，可按事件号 #" + r.incident_id + " 查找", "ok"); } }, `查看原事件 #${r.incident_id}`)) : null,
      canWrite ? h("div", { class: "detail-actions" },
        h("button", { class: "btn", onclick: () => editor(r) }, "编辑")) : null);
  }

  function editor(r) {
    const isNew = !r;
    const sym = h("input", { class: "vs-input", value: r?.symptom || "", placeholder: "例如：订单提交报连接池耗尽，前端 5xx" });
    const rc = h("input", { class: "vs-input", value: r?.root_cause || "", placeholder: "例如：慢查询占满连接池（未加索引）" });
    const res = h("textarea", { class: "vs-input", rows: "9", minlength: "0", placeholder: "可选：怎么解决的（排查步骤 / 处置动作 / 验证口径）。写清楚下次才有参考价值" }, r?.resolution_md || "");
    const tagsIn = h("input", { class: "vs-input mono", value: tagsArr(r?.tags).join(","), placeholder: "逗号分隔，如：mysql,连接池" });
    const en = h("input", { type: "checkbox", checked: r ? (r.enabled ? "checked" : null) : "checked" });
    drawer(isNew ? "新建案例卡" : `编辑案例卡 #${r.id}`,
      note("案例卡会被**向量检索**用于诊断参考，所以症状与根因要写「可检索」的话（含关键错误与现象），"
        + "别只写「服务不可用」这种泛化描述——泛化文本会污染检索（文本太短时平台会刻意不写向量）。"),
      frow(reqLabel("症状（现象）"), sym),
      frow(reqLabel("根因"), rc),
      frow("处置 / 解决方案", res),
      frow("标签（可选）", tagsIn),
      r ? h("div", { class: "form-row" }, h("label", {}, "启用（不参与检索则关掉）"), en) : null,
      h("div", { class: "detail-actions" },
        h("button", { class: "btn", onclick: async (e) => {
          const bad = needText(sym.value, "症状", 500) || needText(rc.value, "根因", 500);
          if (bad) { toast(bad, "error"); return; }
          const tags = tagsIn.value.split(",").map((x) => x.trim()).filter(Boolean);
          try {
            await busy(e.currentTarget, "保存中…", () => (isNew
              ? api.post("/api/ops/case-cards", { symptom: sym.value.trim(), root_cause: rc.value.trim(),
                  resolution_md: res.value, tags })
              : api.patch(`/api/ops/case-cards/${r.id}`, { symptom: sym.value.trim(), root_cause: rc.value.trim(),
                  resolution_md: res.value, tags, enabled: !!en.checked })));
            toast(isNew ? "已创建（文本够长会自动生成向量，参与语义检索）" : "已保存（改症状/根因会重算向量）", "ok");
            document.querySelector(".detail-panel .detail-head .btn")?.click();
            draw();
          } catch (err) { toast(`保存失败：${err.message}`, "error"); }
        } }, isNew ? "创建" : "保存")));
  }

  function frow(label, el) { return h("div", { class: "form-row" }, h("label", {}, label), el); }
  function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v)); }
  const reqLabel = (t) => h("span", { class: "req-wrap", title: "必填" }, t, h("span", { class: "req" }, "*"));

  await draw();
}

export function destroy() {}
