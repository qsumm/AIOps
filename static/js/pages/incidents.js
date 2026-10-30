// pages/incidents.js —— 问题看板（FR-F ops-incidents）
// 必备：incident 卡片（severity/状态/成员告警数）、AI 报告渲染（Markdown + 引用可点击）、
//       一键诊断按钮、合并/拆分
import { api, getUser, can } from "../api.js";
import { marked } from "/vendor/marked.esm.js";
import { h, table, sevBadge, badge, statusBadge, drawer, toast, relTime, fmtTime, select, hostLabel, ensureHostNames, busy, fill, helpTip, pager, note, closeDrawer } from "../ui.js";

let es = null;

const md = (text) => {
  try {
    const raw = marked.parse(text || "", { breaks: true, gfm: true });
    // 引用可点击：半角 (step_12) 与全角 （step_12） 都要能点
    return String(raw).replace(/[（(]step[_-]?(\d+)[)）]/g,
      (m, n) => `<a class="cite" data-step="${n}" href="javascript:void(0)">step_${n}</a>`);
  } catch { return `<pre>${String(text || "").replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]))}</pre>`; }
};

/* P1-10：引用回放。报告里的 step_N 链接此前只有样式没有行为，点了没反应。
   统一在 document 上做一次事件委托：找到对应工具瀑布行 → 滚动 + 高亮。
   选择器带 #inc-steps 限定，避免影响报告正文里同名的锚点。 */
document.addEventListener("click", (e) => {
  const a = e.target.closest && e.target.closest(".cite");
  if (!a) return;
  const n = String(a.dataset.step || "").trim();
  if (!n) return;
  const cell = document.querySelector(`#inc-steps [data-step="${n}"]`);
  if (!cell) { toast(`没有找到 step_${n} 的工具调用记录`, "warn"); return; }
  const row = cell.closest("tr");
  if (row) {
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.add("cite-target");
    setTimeout(() => row.classList.remove("cite-target"), 2200);
  }
});

function showStep(s) {
  const pretty = (v) => {
    try { return typeof v === "string" ? v : JSON.stringify(v, null, 2); }
    catch { return String(v); }
  };
  drawer(`工具调用 · step_${s.step_index}`,
    h("div", { class: "note" }, `工具：${s.tool_name} · 状态：${s.status} · 耗时：${s.duration_ms ?? "-"}ms`),
    h("h3", {}, "参数"), h("pre", { class: "step-io" }, pretty(s.tool_input || {})),
    h("h3", {}, "输出"), h("pre", { class: "step-io" }, pretty(s.tool_output || "(无输出)")));
}

/** 从 AI 报告正文提取「结论」段（种子/导入报告没有 root_cause 列时的兜底）。兼容旧的 ## Summary。 */
function summaryFromReport(report) {
  if (!report) return "";
  const m = /##\s*(结论|Summary)\s*\n([\s\S]*?)(?=\n##\s|$)/i.exec(report);
  return (m ? m[2] : "").trim();
}

/** 把 AI 根因转成参考区可读纯文本：只去 Markdown 标记，保留 bad_var_xyz 这类标识符。 */
function aiPlainText(text) {
  return String(text || "")
    .replace(/```[a-z]*\n?/gi, "")
    .replace(/\*\*|\*/g, "")
    .replace(/`/g, "")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^>\s?/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** 从「验证过的修复动作」段提取建议项（只取一级 bullet，最多 3 条，每条截 160 字）。兼容旧的 ## VALIDATED FIXES。 */
function aiFixBullets(report) {
  if (!report) return [];
  let idx = report.indexOf("## 验证过的修复动作");
  if (idx < 0) idx = report.indexOf("VALIDATED FIXES");
  if (idx < 0) return [];
  const sec = report.slice(idx).replace(/^##\s*(验证过的修复动作|VALIDATED FIXES)\s*\n?/i, "").split(/\n##\s/)[0];
  return sec.split(/\r?\n/)
    .filter((l) => /^\s*-\s+/.test(l))
    .map((l) => l.replace(/^\s*-\s+/, "").replace(/`/g, "").replace(/\*\*/g, "").trim())
    .filter(Boolean)
    .slice(0, 3)
    .map((x) => (x.length > 160 ? x.slice(0, 160) + "…" : x));
}

/** 拆出 YAML frontmatter 与正文：元数据单独成卡片，正文才走 Markdown
 *  （否则开头 --- 会被 marked 渲染成水平线 + 裸文本，实测如此） */
function splitFindings(text) {
  const m = /^\s*---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/.exec(text || "");
  if (!m) return { meta: null, body: text || "" };
  const meta = {};
  let block = null;
  for (const line of m[1].split("\n")) {
    if (!line.trim()) continue;
    if (/^\s{2,}/.test(line) && block) { const i = line.trim().indexOf(":"); block[line.trim().slice(0, i)] = line.trim().slice(i + 1).trim(); continue; }
    const idx = line.indexOf(":");
    if (idx < 0) continue;
    const k = line.slice(0, idx).trim(), v = line.slice(idx + 1).trim();
    if (!v) { block = {}; meta[k] = block; } else { meta[k] = v; block = null; }
  }
  return { meta, body: m[2] };
}

/** 轻量行内列表解析：前端只读展示用，容忍 "[]"、"{a: 1}"、逗号串等写法 */
function toArr(v) {
  if (Array.isArray(v)) return v;
  if (v === null || v === undefined || v === "") return [];
  const s0 = String(v).trim();
  if (s0 === "[]" || s0 === "{}") return [];
  const body = s0.startsWith("[") && s0.endsWith("]") ? s0.slice(1, -1) : s0;
  const out = []; let buf = ""; let depth = 0;
  for (const ch of body) {
    if (ch === "{" || ch === "[") depth++;
    if (ch === "}" || ch === "]") depth--;
    if (ch === "," && depth === 0) { if (buf.trim()) out.push(item(buf.trim())); buf = ""; continue; }
    buf += ch;
  }
  if (buf.trim()) out.push(item(buf.trim()));
  return out;
  function item(x) {
    if (x.startsWith("{") && x.endsWith("}")) {
      const o = {};
      for (const kv of x.slice(1, -1).split(",")) {
        const i = kv.indexOf(":");
        if (i > 0) o[kv.slice(0, i).trim()] = kv.slice(i + 1).trim();
      }
      return o;
    }
    return x;
  }
}

function impactRows(meta) {
  const rows = [];
  const imp = meta?.impact || {};
  const push = (label, cls, arr) => toArr(arr).forEach((x) => rows.push({
    label, cls,
    text: (x && typeof x === "object") ? `${x.entity || ""}${x.evidence_token ? `（${x.evidence_token}）` : ""}` : String(x),
  }));
  push("受影响", "FAIL", imp.affected);
  push("已查正常", "PASS", imp.checked_healthy);
  push("窗口内另有原因", "WARN", imp.changed_other_reason);
  push("未检查", "SKIP", imp.not_checked);
  return rows;
}

/** 把内部收敛标记转成人话（D44：不向用户暴露实现细节）。
    如 manual_split(拆出 abc) → "人工拆分"；merged_by=llm → "智能合并"。 */
function humanReason(raw) {
  const r = String(raw || "");
  if (!r) return "-";
  if (r.startsWith("manual_split")) return "人工拆分";
  if (r.startsWith("manual_merge")) return "人工合并";
  if (r.startsWith("merged_by=llm")) return "智能合并（同一根因）";
  if (r.startsWith("merged_by=rule")) return "规则合并（同主机同时段）";
  if (r.startsWith("merged_by=")) return "自动合并";
  if (r.includes("demo_seed")) return "演示数据";
  return "自动收敛";
}

export async function render(root, sub = "") {
  const me = getUser() || {};
  const canWrite = can("alert_ops");   // 事件认领/关闭/合并/拆分：admin/member
  const canDiag = can("diag");         // 诊断：admin/member
  const state = { status: "" , page: 1, pageSize: 20 };
  let allItems = [];   // 当前列表（合并对话框需要选目标事件）
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));
  await ensureHostNames();   // D40：ident → 主机名

  const head = h("div", { class: "page-head" },
    h("div", { class: "actions" },
      select([["", "全部状态"], ["open", "待处理"], ["ack", "已确认"], ["resolved", "已解决"]], "", {
        onchange: (e) => { state.status = e.target.value; state.page = 1; draw(); } }),
      h("button", { class: "btn secondary", onclick: draw }, "刷新")));
  const body = h("div", { id: "inc-body" });
  const pagerEl = h("div", { class: "pager" });   // D128
  root.replaceChildren(head, body, pagerEl)

  async function draw() {
    let d;
    try { d = await api.get("/api/ops/incidents", { status: state.status, page: state.page, page_size: state.pageSize }); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    const items = d.items || [];
    allItems = items;
    if (!items.length) {
      body.replaceChildren(h("div", { class: "empty" }, "暂无故障事件——告警触发后会自动收敛为事件"));
      return;
    }
    // D62：顶部给一条轻计数（第 62 号打磨：页面要自己会说话，别让用户数卡片）
    const openN = items.filter((i) => i.status !== "resolved").length;
    body.replaceChildren(
      h("div", { class: "count-line muted" },
        `共 ${items.length} 个事件 · 待处理 ${openN}`),
      h("div", { class: "cards inc-grid" }, ...items.map((i) => h("div", { class: `card clickable inc-card sev-${i.severity}`, onclick: () => detail(i, canWrite, canDiag) },
        h("div", { class: "inc-head" }, h("span", { class: "inc-title" }, i.title), sevBadge(i.severity)),
        // D175-5（用户："无法看到是啥问题啊，问题特征，最关键的是啥，没有体现"）：
        // 原来只有规则名，看不出**触发了什么值**。这里给「指标中文名 + 读数」。
        (i.active_services && i.active_services.length) ? h("div", { class: "inc-reason" },
          (i.active_services || []).slice(0, 2).map((s) => h("span", { class: "tag sev", title: s.rule_name || "" },
            // 注意括号：`a || b + c` 会被解析成 `a || (b + c)`——标签存在时读数就被吞掉了
            // （D175-5 上线首日公网截图发现：只有"磁盘 data 分区使用率"、没有 91.0）
            (s.metric_label || s.metric || "")
            + (s.trigger_value !== undefined && s.trigger_value !== null ? ` ${s.trigger_value}` : "")))
        ) : null,
        // 已有 AI 结论 → 一句话点出"最关键的是什么"（详情里有完整证据链）
        i.root_cause_head ? h("div", { class: "inc-headline", title: i.root_cause_head },
          h("span", { class: "inc-headline-tag" }, "结论"), i.root_cause_head) : null,
        h("div", { class: "inc-meta" },
          // D23 配色降噪（P1）：红只留给 severity。open 是"进行中"——蓝虚线。
          statusBadge(i.status, i.status === "open" ? "PROGRESS" : undefined),
          i.confidence ? badge(
            i.confidence === "CONFIRMED" ? "已确认" : i.confidence === "LIKELY" ? "较可能" : "无法确定",
            i.confidence === "CONFIRMED" ? "PASS" : i.confidence === "LIKELY" ? "WARN" : "SKIP") : null,
          h("span", { class: "muted" }, `成员告警 ${i.member_count}`),
          h("span", { class: "muted" }, relTime(i.created_at))),
        h("div", { class: "inc-host mono muted" }, hostLabel(i.host_ident)),
        i.has_report ? h("div", { class: "inc-flag" }, "✓ 已有 AI 报告") : h("div", { class: "inc-flag muted" }, "未诊断")))));
    pagerEl.replaceChildren(pager({ total: d.total || 0, page: state.page, pageSize: state.pageSize, unit: "个",
      onChange: (p, ps) => { state.page = p; if (ps !== state.pageSize) state.pageSize = ps; draw(); } }));
  }

  /** 生成变更单按钮的可用性：报告无建议段时禁用并给出原因（后端同一判定 has_fixes）。
   *  D104 修复：禁用态改为「可点但点了必给原因」——纯 disabled 让用户以为按钮坏了
   *  （真机反馈："点击生成变更单没有反应"），原因必须以 toast 显式弹出，不靠 hover title。 */
  function syncGenBtn(hasFixes) {
    const b = document.getElementById("btn-gen-rb");
    if (!b) return;
    b.dataset.ready = hasFixes ? "1" : "0";
    b.title = hasFixes ? "报告已给出验证过的修复建议，一键生成变更单"
                       : "报告里没有可执行的修复建议，无法生成变更单";
    b.style.opacity = hasFixes ? "1" : "0.5";
    b.style.cursor = hasFixes ? "not-allowed" : "pointer";
  }

  function markNoFixes() {
    const box = document.getElementById("inc-report");
    if (!box || box.querySelector(".no-fixes-tip")) return;
    box.append(note(
      "本次报告**没有「已验证的修复建议」段**：说明 AI 没有给出经过只读命令验证过的修复动作（常见于暂无法定位结论的情况）。" +
      "因此「生成变更单」不可用——这是刻意的：平台只把**经过验证**的建议转成变更单，不拿猜测凑数。",
      "note no-fixes-tip"));
  }

  /** 从诊断报告生成变更单（五段）；成功后跳到处置页看产物。 */
  async function genRunbook(inc) {
    // 未诊断 / 报告无已验证建议 → 不发请求，点了就弹具体原因（D104：禁用也要"点了有反应"）
    const inc0 = inc;
    if (!inc0.has_report) {
      toast("这个事件还没做过 AI 诊断——先点「一键诊断」，出报告后才能生成变更单", "error");
      return;
    }
    if (document.getElementById("btn-gen-rb")?.dataset.ready === "0") {
      toast("本次诊断报告里没有「已验证的修复建议」段，无法生成变更单（平台不拿猜测凑数）。" +
            "常见原因：AI 结论为“暂无法定位”。可重新诊断或先人工处置。", "error");
      return;
    }
    // D51：调模型生成五段报告，通常 10~20s → busy() 负责禁用 + 转圈 + 文案 + 已用秒数
    try {
      const r = await busy("btn-gen-rb", "生成中…", () => api.post(`/api/ops/runbooks/generate/${inc0.id}`));
      toast(`已生成变更单 #${r.runbook_id}（${r.fixes ?? ""}）`, "ok");
      closePanel();
      location.hash = "#/runbooks";
    } catch (e) {
      toast(`生成失败：${e.message}`, "error");
    }
  }

  /** D113：人工订正 AI 结论。保留 AI 原文，另沉淀人工案例卡（human_corrected 标签）。
   *  弹窗顶部默认展示 AI 原结论——否则用户不知道"在订正什么"。 */
  function correctDialog(inc) {
    let aiPlain = "";   // AI 根因纯文本（用于拦截"原样提交，不算订正"）
    const root = h("textarea", { class: "vs-input corr-main", id: "corr-root", rows: "6",
      maxlength: "4000", placeholder: "真实根因（必填）。例：nginx 配置目录被写入非法指令，导致启动失败" });
    const res = h("textarea", { class: "vs-input corr-mid", id: "corr-res", rows: "5",
      maxlength: "4000", placeholder: "实际处置（选填）。例：移出 /etc/nginx/conf.d/test.conf 后 systemctl start nginx" });
    const note = h("textarea", { class: "vs-input corr-note", id: "corr-note", rows: "3",
      maxlength: "500", placeholder: "订正理由（选填）。例：AI 判为资源问题，实际是配置错误" });

    // 字段头：label + 实时字数；粘贴超限时明确提示（maxlength 会静默截断）
    const corrField = (labelText, input, max, required = false) => {
      const cnt = h("span", { class: "corr-count" }, `0 / ${max}`);
      const update = () => {
        const n = (input.value || "").length;
        cnt.textContent = `${n} / ${max}`;
        cnt.classList.toggle("full", n >= max);
      };
      input.addEventListener("input", update);
      input.addEventListener("paste", (e) => {
        const t = (e.clipboardData && e.clipboardData.getData("text")) || "";
        const start = input.selectionStart ?? 0;
        const end = input.selectionEnd ?? 0;
        const expected = (input.value || "").length - (end - start) + t.length;
        if (expected > max) {
          toast(`粘贴内容约 ${expected} 字，超过 ${max} 字上限，将截断保存，请检查`, "warn");
        }
      });
      update();
      return h("div", { class: "corr-field" },
        h("div", { class: "corr-field-head" },
          h("label", { for: input.id }, required ? h("span", { class: "req" }, "*") : null, labelText),
          cnt),
        input);
    };
    const aiBox = h("div", { class: "corr-ai" },
      h("div", { class: "corr-ai-head" }, h("span", {}, "AI 原结论"),
        h("span", { class: "muted", style: "font-weight:400;font-size:0.76rem" }, "读取中…")));
    drawer(`订正结论 · 事件 #${inc.id}`,
      aiBox,
      h("div", { class: "corr-form" },
        corrField("真实根因", root, 4000, true),
        corrField("实际处置", res, 4000),
        corrField("订正理由", note, 500)),
      h("div", { class: "detail-actions" },
        h("button", { class: "btn", onclick: async (e) => {
          const v = (root.value || "").trim();
          if (!v) return toast("真实根因必填", "warn");
          if (aiPlain && v.replace(/\s+/g, " ").trim() === aiPlain) {
            return toast("内容与 AI 原结论相同，不算订正；如确认无误可直接关闭事件", "warn");
          }
          try {
            await busy(e.currentTarget, "提交中…", () => api.post(`/api/ops/incidents/${inc.id}/correct`,
              { root_cause: v, resolution: res.value || "", note: note.value || "" }));
            toast("已订正并沉淀为人工案例", "ok");
            closePanel(); await draw();
          } catch (err) { toast(`订正失败：${err.message}`, "error"); }
        } }, "提交订正"),
        h("button", { class: "btn ghost", onclick: () => closePanel() }, "取消")),
      h("div", { class: "corr-hint" },
        "订正", h("b", {}, "不会覆盖"), " AI 原报告——它会作为人工案例入库，后续诊断优先召回。"));

    // 异步读取事件详情：填充 AI 结论 + 建议处置；失败不挡人工填写
    (async () => {
      const full = await api.get(`/api/ops/incidents/${inc.id}`).catch(() => null);
      const conf = (full && full.confidence) || inc.confidence || "";
      const report = (full && full.ai_report_md) || "";
      const aiRoot = String((full && full.root_cause) || "").trim();
      const aiSummary = aiRoot || summaryFromReport(report);
      const fixes = aiFixBullets(report);
      // 纯文本摘要：参考区不渲染粗体/行内代码，但保留技术标识符（bad_var_xyz）
      const plain = aiPlainText(aiSummary);
      // 与「填入根因框」的截断口径一致：AI 结论超过 2000 字时，原样提交也要被拦住
      aiPlain = plain.slice(0, 4000).replace(/\s+/g, " ").trim();
      const confTip = { CONFIRMED: "证据链闭合：日志/指标/变更全部对上",
                        LIKELY: "证据强指向，但缺最后一环",
                        INCONCLUSIVE: "暂无法定位——这也是合法结论，平台不编造" }[conf] || "";
      const head = h("div", { class: "corr-ai-head" },
        h("span", { class: "corr-ai-title" }, "AI 原结论"),
        conf ? h("span", { title: confTip }, badge(conf)) : h("span", { class: "muted" }, "未诊断"));
      const body = plain
        ? h("div", { class: "corr-ai-text clamp" }, plain)
        : h("div", { class: "corr-ai-empty" },
            report ? "AI 报告没有给出可用的根因结论（可能为 INCONCLUSIVE），可直接记录你的判断。"
                   : "该事件尚未诊断，没有 AI 结论可参照；可直接记录人工判断。");
      let expanded = false;
      const tools = plain
        ? h("div", { class: "corr-ai-tools" },
            plain.length > 180 ? h("button", { class: "btn ghost corr-ai-toggle", onclick: (e) => {
              expanded = !expanded;
              body.classList.toggle("clamp", !expanded);
              e.currentTarget.textContent = expanded ? "收起" : "展开全文";
            } }, "展开全文") : h("span"),
            h("button", { class: "btn ghost corr-ai-use", onclick: () => {
              root.value = plain.slice(0, 4000);
              root.focus();
              root.dispatchEvent(new Event("input"));
              toast("已填入根因框，请按实际情况修改后再提交", "ok");
            } }, "填入根因框作底稿"))
        : null;
      const fixBox = fixes.length
        ? h("div", { class: "corr-ai-fixes" },
            h("div", { class: "corr-ai-label" }, "AI 建议处置",
              h("span", { class: "corr-ai-count" }, `${fixes.length} 条 · 节选`)),
            ...fixes.map((x, i) => h("div", { class: "corr-ai-fix", title: x },
              h("span", { class: "corr-ai-fix-n" }, String(i + 1)),
              h("span", { class: "corr-ai-fix-t" }, x))))
        : null;
      aiBox.replaceChildren(head, body, ...(tools ? [tools] : []), ...(fixBox ? [fixBox] : []));
    })();
  }

  /** 展示该事件已有的人工订正（绿色卡，明确标注"人工"）。 */
  async function drawCorrections(inc) {
    const box = document.getElementById("inc-corrections");
    if (!box) return;
    const d = await api.get(`/api/ops/incidents/${inc.id}/corrections`).catch(() => null);
    const items = (d && d.items) || [];
    if (!items.length) { box.replaceChildren(); return; }
    box.replaceChildren(h("div", { class: "card", style: "border-left:3px solid var(--pass)" },
      h("h3", {}, `人工订正（${items.length}）`, helpTip("这是人工给出的结论，不是 AI 推断；已沉淀进案例库，后续诊断会召回。")),
      ...items.map((c) => h("div", { class: "md-body", style: "margin-bottom:10px" },
        h("div", { class: "muted", style: "font-size:0.78rem" }, fmtTime(c.created_at)),
        h("div", {}, h("b", {}, "真实根因："), c.root_cause || ""),
        c.resolution_md ? h("div", { class: "md-body" }, c.resolution_md) : null))));
  }

  async function detail(inc, canWrite, canDiag) {
    const panel = drawer(`事件 #${inc.id} · ${inc.title}`,
      h("div", { class: "detail-meta" },
        kv("严重级别", sevBadge(inc.severity)), kv("状态", statusBadge(inc.status)),
        kv("主机", h("span", { class: "mono" }, hostLabel(inc.host_ident))),
        kv("置信度", inc.confidence ? h("span", { title: { CONFIRMED: "证据链闭合：日志/指标/变更全部对上", LIKELY: "证据强指向，但缺最后一环", INCONCLUSIVE: "暂无法定位——这也是合法结论，平台不编造" }[inc.confidence] || "" }, badge(inc.confidence)) : h("span", { class: "muted" }, "-")),
        kv("收敛方式", h("span", { class: "muted" }, humanReason(inc.cluster_reason))),
        kv("首次发生", fmtTime(inc.created_at))),
      h("div", { class: "detail-actions" },
        canDiag ? h("button", { class: "btn", id: "btn-diag", onclick: () => runDiag(inc) }, "一键诊断（AI）") : null,
        // 「生成变更单」：报告里必须有显式建议段（VALIDATED FIXES）——
        // 后端同一判定（has_fixes）。无建议段时按钮仍可点，点击即弹具体原因
        // （D104 真机反馈：纯 disabled 让用户以为按钮坏了）；
        // 后端对没有建议段的生成请求返回 NO_VALIDATED_FIX，不会凭空造步骤。
        canWrite ? h("button", { class: "btn secondary", id: "btn-gen-rb",
            title: inc.has_fixes ? "报告已给出验证过的修复建议，一键生成变更单"
                                 : "报告里没有可执行的修复建议，无法生成",
            onclick: () => genRunbook(inc) }, "生成变更单", helpTip("把报告「建议段」里**验证过的**动作转成可执行变更单（带审批与审计留痕）。报告里没有建议段时按钮不可用——平台不会凭空造步骤。")) : null,
        // D113：人工订正 AI 结论（不覆盖原文；沉淀人工案例卡供后续诊断召回）
        canWrite ? h("button", { class: "btn secondary", id: "btn-correct",
            title: "如果 AI 的根因判断不对：写下真实根因与实际处置，平台会把它作为人工案例沉淀",
            onclick: () => correctDialog(inc) }, "订正结论") : null,
        canWrite && inc.status !== "resolved" ? h("button", { class: "btn secondary", onclick: async (e) => { try { await busy(e.currentTarget, "关闭中…", () => api.post(`/api/ops/incidents/${inc.id}/close`)); toast("已关闭", "ok"); closePanel(); draw(); } catch (err) { toast(`失败：${err.message}`, "error"); } } }, "关闭事件") : null,
        canWrite && inc.status === "open" ? h("button", { class: "btn ghost", onclick: async (e) => { try { await busy(e.currentTarget, "确认中…", () => api.post(`/api/ops/incidents/${inc.id}/ack`)); toast("已确认", "ok"); closePanel(); draw(); } catch (err) { toast(`失败：${err.message}`, "error"); } } }, "确认") : null,
        // D29：手册 §6.4 承诺「手动合并/拆分」，后端 API 早已存在但前端从未接入 → 补上
        canWrite ? h("span", { class: "tip-inline" },
          h("button", { class: "btn ghost", onclick: () => mergeDialog(inc) }, "合并到其他事件"),
          helpTip("把两个事件并成一个：用于**同一根因**被拆成多条事件的情况（成员告警、报告、时间线一起归并）。点开后会让你选合并到哪个事件。")) : null),
      h("div", { class: "card" }, h("h3", {}, "AI 诊断报告"),
        h("div", { id: "inc-triage", style: "margin-bottom:10px" }),
        h("div", { id: "inc-report", class: "md-body report-wide" },
        // D22 修复：空态提示必须与按钮的**能力门禁**一致 —— 原先无条件写
        // "点上方「一键诊断」"，但该按钮对 demo 是隐藏的（diag 能力位），
        // 演示用户被指引去找一个不存在的按钮（用户实测报障，2026-10-14）
        h("div", { class: "empty" }, canDiag
          ? "尚未诊断——点上方「一键诊断（AI）」"
          : "尚未诊断。AI 诊断需要 admin / member 权限；演示账号可在「AI 助手」就本事件提问，获得同样的只读分析。"))),
      h("div", { id: "inc-corrections" }),
      h("div", { class: "card" }, h("h3", {}, "错误上下文（日志证据）"),
        h("div", { id: "inc-context" }, h("div", { class: "empty" }, "加载中…")),
        canDiag ? h("div", { class: "detail-actions", style: "margin-top:10px" },
          h("button", { class: "btn secondary", onclick: (e) => fetchContextLogs(inc, e.currentTarget) },
            "取证：取回这个时间窗的日志")) : null),
      h("div", { class: "card" }, h("h3", {}, "工具瀑布（三层留痕）"), h("div", { id: "inc-steps" }, h("div", { class: "empty" }, "-"))),
      h("div", { class: "card" }, h("h3", {}, `成员告警（${(inc.members || []).length}）`),
        h("div", { id: "inc-members" }, h("div", { class: "empty" }, "-"))));

    drawContext(inc);
    drawCorrections(inc);

    // 已有报告 → 直接渲染。
    // D58：报告有**两个来源**——findings 表（真诊断产出）与事件表 ai_report_md（演示种子/导入）。
    // 此前只读 findings → 列表按 ai_report_md 判"已有 AI 报告"、详情却显示"尚未诊断"（用户实测 #5）。
    const full = await api.get(`/api/ops/incidents/${inc.id}`).catch(() => null);
    const f = await api.get(`/api/ops/incidents/${inc.id}/findings`).catch(() => null);
    if (f && f.findings_md) {
      renderReport(f);
      renderTriage(inc, f);      // D170（FR-426）：发现分诊——处置按钮 + 降噪提示
      // 报告是刚产出的 → 用后端同一份判定（has_fixes）刷新按钮可用性，避免前后端两套规则
      syncGenBtn(full?.has_fixes ?? inc.has_fixes);
    } else if (full && full.ai_report_md) {
      renderReport({ findings_md: full.ai_report_md, steps: [], total_tokens: null });
      syncGenBtn(full.has_fixes);
    }
    syncGenBtn(inc.has_fixes);
    if (inc.has_report && !inc.has_fixes) markNoFixes();
    if (full) {
      document.getElementById("inc-members")?.replaceChildren(table([
        { title: "规则", render: (r) => r.rule_name },
        { title: "指标", width: "130px", render: (r) => h("span", { class: "mono" }, r.metric) },
        { title: "值", width: "90px", render: (r) => h("span", { class: "mono" }, r.trigger_value) },
        { title: "时间", width: "140px", render: (r) => h("span", { class: "muted mono" }, fmtTime(r.trigger_time)) },
        { title: "", width: "80px", render: (r) => badge(r.is_recovered ? "已恢复" : "活跃", r.is_recovered ? "PASS" : "FAIL") },
        canWrite ? { title: ["操作", helpTip("拆分 = 把这条成员告警拆成独立事件（成员、报告、时间线跟着走）。用于自动收敛判错、或把不相关告警混进同一事件的情况。")], width: "72px", render: (r) => h("button", { class: "btn ghost", title: "把这条成员告警拆分为独立事件（收敛判错时用）",
            onclick: async (e) => {
              try { await busy(e.currentTarget, "拆分中…", () => api.post(`/api/ops/incidents/${inc.id}/split`, { alert_hash: r.hash })); toast("已拆分为独立事件", "ok"); closePanel(); draw(); }
              catch (err) { toast(`拆分失败：${err.message}`, "error"); }
            } }, "拆分") } : null,
      ].filter(Boolean), full.members, { emptyText: "无成员" }));
    }
    return panel;
  }

  /** 合并对话框（D29）：列出其他未解决事件，选中即把当前事件并入目标事件。 */
  function mergeDialog(inc) {
    const others = allItems.filter((x) => x.id !== inc.id && x.status !== "resolved");
    drawer(`合并事件 #${inc.id} 到…`,
      h("div", { class: "note" }, "选择目标事件：本事件（被合并方）的成员告警将归入目标事件，本事件随即消失。"),
      others.length
        ? h("div", { class: "cards" }, ...others.map((o) => h("div", { class: "card clickable",
            onclick: async (e) => {
              try { await busy(e.currentTarget, "合并中…", () => api.post("/api/ops/incidents/merge", { keep_id: o.id, merge_id: inc.id })); toast("已合并", "ok"); closePanel(); draw(); }
              catch (e) { toast(`合并失败：${e.message}`, "error"); }
            } },
            h("div", { class: "inc-head" }, h("span", { class: "inc-title" }, `#${o.id} ${o.title}`), sevBadge(o.severity)),
            h("div", { class: "inc-meta" }, statusBadge(o.status, o.status === "open" ? "PROGRESS" : "WARN"),
              h("span", { class: "muted" }, `成员告警 ${o.member_count}`),
              h("span", { class: "mono muted" }, hostLabel(o.host_ident))))))
        : h("div", { class: "empty" }, "没有其他可合并的事件"));
  }

  /** P5b（FR-410）：错误上下文——错误簇（含栈指纹）+ 多源时间线 + 关联指标。 */
  async function drawContext(inc) {
    const box = document.getElementById("inc-context");
    if (!box) return;
    let d = null;
    try { d = await api.get(`/api/ops/incidents/${inc.id}/context`); } catch { /* ignore */ }
    if (!d) { box.replaceChildren(h("div", { class: "empty" }, "上下文暂不可用")); return; }
    const clusters = d.clusters || [];
    const tl = d.timeline || { items: [] };
    if (!clusters.length && !(tl.items || []).length) {
      box.replaceChildren(h("div", { class: "muted" },
        "暂无日志证据与时间线——可点下方「取证」按故障时间窗取回相关日志片段"
        + "（需目标机 Agent ≥0.3.0，且服务已声明日志路径）"));
      return;
    }
    fill(box,
      clusters.length ? h("div", {},
        h("div", { class: "muted", style: "font-size:0.78rem;margin-bottom:6px" },
          `聚出 ${clusters.length} 个候选错误簇（按首次出现排序，首错优先）：`),
        ...clusters.slice(0, 5).map((c, i) => h("div", { class: "ctx-cluster" },
          h("div", { class: "ctx-head" },
            h("span", { class: "ctx-idx" }, String(i + 1)),
            h("span", { class: "mono" }, c.signature),
            h("span", { class: "muted" }, `命中 ${c.count} 次`),
            c.first_ts ? h("span", { class: "muted mono", style: "font-size:0.74rem" }, `首见 ${String(c.first_ts).slice(5, 19)}`) : null,
            (c.hosts || []).length ? h("span", { class: "muted" }, `· ${c.hosts.slice(0, 2).join(", ")}`) : null),
          c.stack ? h("div", { class: "muted mono", style: "font-size:0.72rem" }, `栈：${c.stack}`) : null,
          h("pre", { class: "log-pre" }, (c.sample || []).slice(0, 4).join("\n"))))) : null,
      (tl.items || []).length ? h("div", { style: "margin-top:10px" },
        h("div", { class: "muted", style: "font-size:0.78rem;margin-bottom:6px" },
          `时间线（${tl.total} 条，多来源已对齐到 UTC${tl.unknown_time ? `；${tl.unknown_time} 条时间未解析` : ""}）：`),
        table([
          { title: "时间(UTC)", width: "150px", render: (r) => h("span", { class: "mono", style: "font-size:0.74rem" }, String(r.ts).slice(5, 19) || "未知") },
          { title: "来源", width: "64px", render: (r) => badge(r.source, "ACCENT") },
          { title: "事件", render: (r) => h("span", {}, r.title) },
          { title: "说明", width: "190px", render: (r) => h("span", { class: "muted", style: "font-size:0.76rem" }, String(r.detail || "").slice(0, 60)) },
        ], tl.items)) : null,
      (d.related_metrics || []).length ? h("div", { class: "muted", style: "font-size:0.76rem;margin-top:8px" },
        "关联指标：" + d.related_metrics.map((m) => `${m.metric}（${m.summary}）`).join("；")) : null);
  }

  /** 以告警首次触发时刻为锚算时间窗（前 5 分钟 ~ 后 2 分钟，前多后少）——
      与服务维度取证同一口径：故障常发生在触发之前，用 now 取尾部会错过成因。 */
  function anchorWindow(anchor) {
    const t = new Date(String(anchor || "").replace(" ", "T").slice(0, 19));
    if (isNaN(t.getTime())) return { since: "", until: "" };
    const fmt = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    return { since: fmt(new Date(t.getTime() - 5 * 60000)), until: fmt(new Date(t.getTime() + 2 * 60000)) };
  }

  /** 按事件的故障时间窗下发取证（锚点用最早成员告警的首次触发时刻）。 */
  async function fetchContextLogs(inc, btn) {
    const anchor = (inc.members || []).map((m) => m.trigger_time).filter(Boolean).sort()[0] || "";
    try {
      const hosts = await api.get("/api/ops/services");
      const mine = (hosts.items || []).filter((s) => s.host === hostLabel(inc.host_ident));
      let sid = mine.length ? mine[0].id : null;
      if (!sid) {
        // 该主机没有已登记服务 → 用主机维度的取证入口（窗口同样以触发时刻为锚）
        const w = anchorWindow(anchor);
        await busy(btn, "下发中…", () => api.post("/api/ops/log-fetch", {
          ident: inc.host_ident, path: "", unit: "", since: w.since, until: w.until,
          origin: `incident:${inc.id}` }));
        toast("已下发取证（该主机未登记服务，按主机维度取；需 Agent ≥0.3.0 与已声明日志路径）", "warn");
        return;
      }
      const r = await busy(btn, "下发中…", () => api.post(`/api/ops/services/${sid}/log-fetch`, {
        anchor: String(anchor).replace(" ", "T").slice(0, 19), window_min: 5, context: 3,
        origin: `incident:${inc.id}` }));
      toast("已下发取证（Agent 约 15 秒内执行，完成后本卡片自动刷新）", "ok");
      // 轮询等结果，完成后重画上下文
      const tid = r.task_id;
      setTimeout(async () => {
        for (let i = 0; i < 20; i++) {
          await new Promise((x) => setTimeout(x, 2000));
          let t = null;
          try { t = await api.get(`/api/ops/tasks/${tid}`); } catch { /* 继续等 */ }
          if (t && (t.status === "done" || t.status === "failed" || t.status === "expired")) break;
        }
        drawContext(inc);
      }, 2000);
    } catch (err) { toast(`下发失败：${err.message}`, "error"); }
  }

  // D170（FR-426）：发现分诊——报告处置（跟进/观察/忽略）+ 降噪提示。
  // 设计取舍：以「一次诊断报告」为分诊单位（rca_findings 粒度=一次运行），
  // 不拆报告内 markdown 条目（LLM 输出格式不稳定，逐条解析是脆的）。
  function renderTriage(inc, f) {
    const box = document.getElementById("inc-triage");
    if (!box) return;
    // D175-1：`/api/ops/incidents/{id}/findings` 返回的是**单条最新诊断**（有 status/
    // triage/finding_id 顶层字段），不是数组。此前这里读 `f.findings`（恒为 undefined →
    // 空数组），于是 `rows[0].status` 恒为空 → **每份成功报告都被判成"生成失败"**
    // （用户实测 #2000065：strong/CONFIRMED 的完整报告下方写着"诊断未产出有效结论"）。
    const cur = f.triage || null;
    const fid = f.finding_id || null;
    const mutedNote = f.triage_muted_note || "";
    async function doTriage(action, reason, btnEl) {
      if (!fid) { toast("找不到这次诊断的编号，无法记录处置——请刷新后重试", "error"); return; }
      try {
        await busy(btnEl, "处置中…", () => api.post(
          `/api/ops/incidents/${inc.id}/findings/${fid}/triage`,
          { action, reason }));
        toast(action === "dismissed" ? "已忽略——后续同类发现会带降噪提示" : "已记录处置", "ok");
        closeDrawer(); closePanel(); draw();
      } catch (err) { toast(`失败：${err.message}`, "error"); }
    }
    function chip(action, label) {
      return h("button", { class: "btn ghost", onclick: (e) => {
        e.stopPropagation();
        if (action !== "dismissed") { doTriage(action, "", e.currentTarget); return; }
        // 忽略：抽屉填理由（必填）——理由用于后续同类发现的降噪提示
        const input = h("textarea", { class: "vs-input", rows: "3",
          placeholder: "例如：已确认是该机器常态水位，无需处理（≤500 字）" });
        drawer("忽略这份诊断报告",
          h("div", { class: "note" }, "忽略必须填写理由——理由会随处置留痕，后续同类发现的降噪提示会引用它。"),
          input,
          h("div", { class: "detail-actions" },
            h("button", { class: "btn", onclick: async (e) => {
              const reason = input.value.trim();
              if (!reason) { toast("忽略必须填写理由", "error"); return; }
              await doTriage("dismissed", reason, e.currentTarget);
            } }, "确认忽略"),
            h("button", { class: "btn ghost", onclick: () => closeDrawer() }, "取消")));
      } }, label);
    }
    // 可分诊 = **有真正的结论正文**（失败 stub 只有一段失败说明，没有可分诊内容）。
    // 只看 status 会误伤两种情况：① 后端状态为 succeeded 但报告是 INCONCLUSIVE stub；
    // ② 状态字段缺失/改名（字段形状一变就全体误判为失败——D175-1 的根因）。
    const md = String(f.findings_md || "");
    const hasConclusion = md.length > 200 && !/^---\nagent_id:[\s\S]*?status:\s*(failed|timeout)/.test(md);
    const sug = cur?.suggested || "";
    // D172-1：replaceChildren(null) 会把 null 渲染成 "null" 文本节点（用户截图实锤）——
    // 元素数组必须 filter(Boolean)。且诊断失败的 stub 报告**没有可分诊的内容**，
    // 不显示跟进/观察/忽略按钮（友好提示：请重新发起诊断）。
    const parts = [];
    if (mutedNote) parts.push(h("div", { class: "note", style: "margin-bottom:8px" }, mutedNote));
    if (!hasConclusion) {
      parts.push(h("div", { class: "note" },
        "这次诊断没有产出有效结论，没有可分诊的内容——请点「一键诊断（AI）」重新发起。"));
    } else {
      parts.push(h("div", { style: "display:flex; align-items:center; gap:8px; flex-wrap:wrap" },
        h("span", { class: "muted", style: "font-size:0.78rem" }, "处置："),
        cur ? badge(
          cur.action === "followup" ? "跟进中" : cur.action === "dismissed" ? "已忽略" : "观察中",
          cur.action === "followup" ? "WARN" : cur.action === "dismissed" ? "SKIP" : "ACCENT",
        ) : h("span", { class: "muted", style: "font-size:0.78rem" }, "未处置"),
        cur?.reason ? h("span", { class: "muted", style: "font-size:0.76rem" }, `理由：${cur.reason.slice(0, 40)}`) : null,
        sug ? h("span", { class: "muted", style: "font-size:0.76rem" }, `AI 强度建议：${sug}`) : null,
        h("span", { style: "flex:1" }),
        canDiag ? h("div", { class: "row-actions" },
          chip("followup", "跟进"),
          chip("observing", "观察"),
          chip("dismissed", "忽略")) : null));
    }
    fill(box, ...parts);   // D172-1：fill 自动丢弃 null——别手写 replaceChildren（DOM 会把 null 印成 "null"）
  }

  function renderReport(f) {
    const el = document.getElementById("inc-report");
    if (!el) return;
    const { meta, body } = splitFindings(f.findings_md);
    const conf = (meta?.confidence || "").replace(/[\[\]"]/g, "");
    const strength = (meta?.self_assessed_strength || "").replace(/[\[\]"]/g, "");
    el.replaceChildren(...[
      meta ? h("div", { class: "findings-meta" },
        h("div", { class: "fm-row" },
          h("span", { class: "muted" }, "置信度"), badge(
            conf === "CONFIRMED" ? "已确认" : conf === "LIKELY" ? "较可能" : "无法确定",
            conf === "CONFIRMED" ? "PASS" : conf === "LIKELY" ? "WARN" : "SKIP"),
          h("span", { class: "muted" }, "自评强度"), h("span", { class: "mono" }, strength || "-"),
          h("span", { class: "muted" }, "工具"), h("span", { class: "mono" }, String(meta.tools_used || "").slice(0, 90)),
          h("span", { class: "muted" }, "Token 消耗"), h("span", { class: "mono" }, f.total_tokens ?? "-")),
        h("div", { class: "fm-row" }, h("span", { class: "muted" }, "引用"),
          ...String(meta.citations || "").replace(/[\[\]]/g, "").split(",").filter(Boolean).slice(0, 10)
            .map((c) => h("a", { class: "cite", href: "javascript:void(0)", "data-step": String(c).replace(/\D/g, "") }, String(c).trim()))),
        h("div", { class: "fm-impact" }, ...impactRows(meta).map((r) => h("div", { class: "fm-impact-row" },
          badge(r.label, r.cls), h("span", { class: "fm-impact-text" }, r.text))))) : null,
      h("div", { class: "md-body report-wide", html: md(body) }),
    ].filter(Boolean));   // D58：replaceChildren(null) 会渲染成文字"null"（种子报告实测）
    document.getElementById("inc-steps")?.replaceChildren(
      table([
        { title: "#", width: "42px", render: (r) => h("span", { class: "mono muted", "data-step": r.step_index }, r.step_index) },
        { title: "工具", width: "128px", render: (r) => h("span", { class: "mono" }, r.tool_name) },
        { title: "参数", render: (r) => h("span", { class: "mono muted" }, JSON.stringify(r.tool_input).slice(0, 60)) },
        { title: "状态", width: "76px", render: (r) => statusBadge(r.status) },
        { title: "耗时", width: "70px", render: (r) => h("span", { class: "mono muted" }, `${r.duration_ms}ms`) },
        { title: "", width: "58px", render: (r) => h("button", { class: "btn ghost",
            onclick: (e) => { e.stopPropagation(); showStep(r); } }, "证据") },
      ], f.steps || [], { emptyText: "无工具调用记录" }));
  }

  async function runDiag(inc) {
    const incidentId = inc.id;
    const btn = document.getElementById("btn-diag");
    if (btn) { btn.disabled = true; btn.textContent = "诊断中…"; }
    const reportEl = document.getElementById("inc-report");
    const stepsEl = document.getElementById("inc-steps");
    const live = h("div", { class: "live-log" });   // 实时工具瀑布
    // D51：诊断是长任务（多步 ReAct，常见 20~60s）→ 标题行带已用时间，用户能确认"在跑"而不是死了
    const waiting = h("div", { class: "muted" }, "AI 正在调查（可实时看到每步工具调用）…");
    if (reportEl) reportEl.replaceChildren(waiting, live);
    const t0 = Date.now();
    const tick = setInterval(() => {
      waiting.textContent = `AI 正在调查中 · 已用 ${Math.round((Date.now() - t0) / 1000)}s（可实时看到每步工具调用）`;
    }, 1000);
    if (stepsEl) stepsEl.replaceChildren(h("div", { class: "muted" }, "见上方实时流"));

    if (es) es.close();
    const sse = new EventSource(`/api/ops/incidents/${incidentId}/diagnose/stream?ticket=${encodeURIComponent(await ticket())}`);
    es = sse;
    const add = (cls, text) => { live.append(h("div", { class: `live-line ${cls}` }, text)); live.scrollTop = live.scrollHeight; };
    // D51：实时流连不上/中途断开时，不能让「诊断中…」永远转下去（中间态必须能收尾）。
    // EventSource 自身会重连（readyState=0），所以只等 CLOSED 不够 —— 连续失败 3 次就收尾，
    // 否则服务端一直 5xx 时界面会无限显示「已用 Ns」（实测）。
    let errs = 0;
    sse.onerror = () => {
      errs += 1;
      if (sse.readyState !== 2 && errs < 3) return;
      clearInterval(tick);
      waiting.textContent = `实时连接已中断（已重试 ${errs} 次）——可点「重新诊断」重试；已产出的步骤仍在上方。`;
      add("warn", "实时连接已断开，已停止等待。");
      if (btn) { btn.disabled = false; btn.textContent = "重新诊断"; }
    };
    sse.addEventListener("thinking", (e) => { const p = JSON.parse(e.data); add("think", `（第 ${p.step} 步）${p.text.slice(0, 200)}`); });
    sse.addEventListener("tool_call", (e) => { const p = JSON.parse(e.data); add("call", `→ ${p.tool} ${JSON.stringify(p.params).slice(0, 100)}`); });
    sse.addEventListener("tool_result", (e) => { const p = JSON.parse(e.data); add(p.status === "SUCCESS" ? "ok" : "nodata", `   ← ${p.status} ${(p.note || "").slice(0, 90)} (${p.elapsed_ms}ms)`); });
    sse.addEventListener("tool_denied", (e) => add("warn", `   ✗ 拒绝重复调用 ${JSON.parse(e.data).tool}`));
    sse.addEventListener("hint", (e) => add("warn", `! ${JSON.parse(e.data).text}`));
    sse.addEventListener("validate_retry", (e) => add("warn", `✗ 报告 schema 校验未过，重试：${JSON.parse(e.data).errors.slice(0, 2).join("；")}`));
    sse.addEventListener("error", (e) => { try { add("warn", `错误：${JSON.parse(e.data).msg}`); } catch { /* ignore */ } });
    sse.addEventListener("diag_done", async (e) => {
      const p = JSON.parse(e.data);
      clearInterval(tick);
      sse.close(); es = null;
      if (btn) { btn.disabled = false; btn.textContent = "重新诊断"; }
      toast(`诊断完成：${p.status} / ${p.confidence}（${p.steps} 步）`, p.status === "succeeded" ? "ok" : "warn");
      const f = await api.get(`/api/ops/incidents/${incidentId}/findings`).catch(() => null);
      if (f) renderReport(f);
      // 全流程实测：诊断完成后 inc 仍是列表里的旧对象，has_report=false →
      // 「生成变更单」误报“还没诊断”。这里用后端最新状态回填并同步按钮。
      const full = await api.get(`/api/ops/incidents/${incidentId}`).catch(() => null);
      if (full) {
        inc.has_report = Boolean(full.ai_report_md);
        inc.has_fixes = Boolean(full.has_fixes);
        syncGenBtn(inc.has_fixes);
      }
      draw();
    });
  }

  const ticket = async () => (await api.get("/api/sse-ticket")).ticket;
  function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v)); }
  const closePanel = () => document.querySelector(".detail-head .btn")?.click();

  await draw();

  // 深链：按事件 id 或告警指纹自动打开事件抽屉
  const deep = String(sub || "").trim();
  if (deep) {
    try {
      let target = null;
      if (deep.startsWith("alert/")) {
        const h = decodeURIComponent(deep.slice("alert/".length)).trim();
        target = await api.get(`/api/ops/incidents/by-alert/${encodeURIComponent(h)}`);
      } else if (/^\d+$/.test(deep)) {
        target = allItems.find((x) => x.id === Number(deep)) || null;
        if (!target) {
          const d = await api.get(`/api/ops/incidents/${Number(deep)}`);
          target = { ...d, member_count: (d.members || []).length,
                     has_report: Boolean(d.ai_report_md), has_fixes: Boolean(d.has_fixes) };
        }
      }
      if (target) detail(target, canWrite, canDiag);
    } catch (e) {
      toast("未找到对应事件（可能还在生成），请稍后刷新或在列表中打开", "warn");
    }
  }
}

export function destroy() { if (es) { es.close(); es = null; } }
