// pages/skills.js —— 技能管理（FR-F ops-skills；D173：自动路由/授权命令/效果度量/GitHub 导入）
// 必备：SKILL.md 列表（内置/自定义/GitHub 导入）、编辑器、启用开关、连接检查状态
import { api, getUser, can } from "../api.js";
import { marked } from "/vendor/marked.esm.js";   // D127：SKILL.md 正文是 markdown，查看时渲染而非露源码
import { h, table, badge, drawer, toast, fmtTime, busy, reqLabel, needText, helpTip } from "../ui.js";

export async function render(root) {
  const me = getUser() || {};
  const canWrite = can("write");       // 技能管理：admin 专属
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  const head = h("div", { class: "page-head" },
    h("div", { class: "actions" },
      h("button", { class: "btn secondary", onclick: (e) => doImport(e.currentTarget) }, "扫描 skills 目录导入"),
      canWrite ? h("button", { class: "btn secondary", onclick: () => githubImporter() }, "从 GitHub 导入") : null,
      canWrite ? h("button", { class: "btn", onclick: () => editor(null) }, "新建技能") : null));
  const tip = h("div", { class: "note" },
    "技能 = AI 诊断时按需使用的运维知识包（如磁盘排查、日志取证）。诊断时 AI 只看每项技能的一句话描述，"
    + "匹配到相关问题才加载完整内容，不盲目套用。带「内置」的是平台自带方法论；「自定义」可以编写自己的排查经验。",
    h("br"),
    h("span", { class: "muted" },
      "填了「适用告警模式」的技能会在告警诊断发起时自动加载（不再靠 AI 自觉）；"
      + "「授权命令」只在该技能被加载的这次诊断内临时可用（只读命令，仍受危险命令黑名单约束）；"
      + "「效果」列的数字来自真实诊断流水，样本不足会如实显示「样本不足」。"));
  const body = h("div", { id: "sk-body" });
  root.replaceChildren(head, tip, body);

  async function draw() {
    let d;
    try { d = await api.get("/api/ops/skills"); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    body.replaceChildren(table([
      { title: "名称", render: (r) => h("span", { class: "mono" }, r.name) },
      { title: "描述", width: "300px", render: (r) => h("span", { class: "sk-desc", title: r.description || "" }, r.description) },
      { title: "来源", width: "96px", render: (r) => sourceBadge(r.source) },
      { title: ["适用告警", helpTip("自动路由范围。诊断发起时，事件的告警指标或规则名命中这里的模式，"
        + "该技能全文会被**自动注入**诊断上下文——不再依赖 AI 自觉去 load_skill。\n"
        + "写法：支持通配（cpu.*、*内存*）或精确名；多个用逗号分隔。留空 = 只在索引里等 AI 主动加载。")],
        width: "180px", render: (r) => (r.alerts_scope || []).length
          ? h("span", { class: "mono muted sk-scope", style: "font-size:0.74rem",
              title: (r.alerts_scope || []).join("、") }, (r.alerts_scope || []).join("、"))
          : h("span", { class: "muted" }, "—") },
      { title: ["效果", helpTip("P4 效果度量：数字全部来自真实诊断流水，可按 execution_steps / finding_triage / "
        + "rca_findings 复算。\n加载 N 次 = 该技能被用过的诊断数；跟进率 = 用了它的诊断中人工处置为「跟进」的比例；"
        + "基线是全平台比例。样本不足 5 例时显示「样本不足」，不硬算比率。\n"
        + "参考指标（相关非因果）：技能常被用在更难的诊断上，跟进率天然偏高。")],
        width: "210px", render: (r) => effectCell(r.effect) },
      { title: ["优先级", helpTip("数字越小越先被注入。诊断 Agent 只注入技能的**描述索引**，需要时才用 load_skill 拉全文——所以描述写准比正文长度更重要（描述缺失=永不匹配）。")], width: "80px", render: (r) => h("span", { class: "mono muted" }, r.rca_priority) },
      { title: ["连接检查", helpTip("检查该技能的 requires 依赖是否就绪（如需要某个数据源或探活项）。**未就绪时不会被注入**——避免让模型去用一个当前不可用的能力。")], width: "130px", render: (r) => {
          const cc = r.connection_check || {};
          const when = (t) => { const d = new Date(String(t || "").replace(" ", "T") + "Z"); return isNaN(d) ? "" : `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${d.toTimeString().slice(0, 5)}`; };
          if (!(r.requires || []).length) return badge("无外部依赖", "SKIP");
          if (cc.ok === true) return badge(`通过 · ${when(cc.checked_at)}`, "PASS");
          if (cc.ok === false) return badge("依赖未就绪", "WARN");
          return badge("未检查", "WARN");
        } },
      { title: "状态", width: "80px", render: (r) => badge(r.enabled ? "启用" : "停用", r.enabled ? "PASS" : "SKIP") },
      { title: "操作", width: "240px", render: (r) => h("div", { class: "row-actions" },
          h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); detail(r.id); } }, "查看"),
          canWrite ? h("button", { class: "btn ghost", onclick: async (e) => { e.stopPropagation();
            try { const t = await busy(e.currentTarget, "检查中…", () => api.post(`/api/ops/skills/${r.id}/check`));
              toast(`${r.name}：${t.ok ? "连接正常" : "依赖未就绪"}${t.note ? " · " + t.note : ""}`, t.ok ? "ok" : "warn"); }
            catch (err) { toast(`检查失败：${err.message}`, "error"); }
            draw(); } }, "连接检查") : null,
          canWrite ? h("button", { class: "btn ghost", onclick: async (e) => { e.stopPropagation();
            try { const t = await busy(e.currentTarget, "处理中…", () => api.post(`/api/ops/skills/${r.id}/toggle`));
              toast(t.enabled ? "已启用" : "已停用", "ok"); } catch (err) { toast(`失败：${err.message}`, "error"); }
            draw(); } }, r.enabled ? "停用" : "启用") : null,
          canWrite ? h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); editor(r); } }, "编辑") : null) },
    ], d.items || [], { emptyText: "暂无技能——点「扫描 skills 目录导入」同步内置核心技能", onRowClick: (r) => detail(r.id) }));
  }

  function sourceBadge(src) {
    if (src === "builtin") return badge("内置", "ACCENT");
    if (src === "imported") return badge("GitHub 导入", "PASS");
    return badge("自定义", "PASS");
  }

  function effectCell(eff) {
    if (!eff) return h("span", { class: "muted" }, "—");
    if (eff.insufficient) {
      return h("div", { class: "sk-effect" },
        h("span", { class: "muted", title: `样本 ${eff.loaded || 0} 例（<5），不计算比率` },
          `样本不足·${eff.loaded || 0} 次`));
    }
    const parts = [h("span", { class: "mono" }, `加载 ${eff.loaded} 次`)];
    if (eff.followup_rate !== undefined && eff.followup_rate !== null) {
      const base = eff.baseline_rate !== null && eff.baseline_rate !== undefined
        ? ` / 基线 ${Math.round(eff.baseline_rate * 100)}%` : "";
      parts.push(h("span", { class: "muted" }, `跟进 ${Math.round(eff.followup_rate * 100)}%${base}`));
    }
    if (eff.token_median) parts.push(h("span", { class: "muted" }, `Token ${eff.token_median}`));
    return h("div", { class: "sk-effect" }, parts);
  }

  async function detail(sid) {
    const s = await api.get(`/api/ops/skills/${sid}`);
    drawer(`技能 · ${s.name}`,
      h("div", { class: "detail-meta" },
        kv("来源", sourceBadge(s.source)),
        kv("优先级", String(s.rca_priority)),
        kv("启用", badge(s.enabled ? "启用" : "停用", s.enabled ? "PASS" : "SKIP"))),
      h("div", { class: "note" }, s.description),
      h("div", { class: "card" }, h("h3", {}, "适用告警模式（自动路由）"),
        (s.alerts_scope || []).length
          ? h("div", {}, h("div", { class: "row-actions" },
            (s.alerts_scope || []).map((p) => badge(p, "ACCENT"))))
          : h("div", { class: "muted" }, "未设置——该技能只出现在索引里，等 AI 主动 load_skill；不会自动注入。"),
        h("div", { class: "muted", style: "margin-top:6px;font-size:0.76rem" },
          "诊断发起时，事件的告警指标或规则名命中上述模式（支持 cpu.* / *内存* 通配）即自动加载该技能全文。")),
      h("div", { class: "card" }, h("h3", {}, "授权命令（会话内只读白名单）"),
        (s.commands || []).length
          ? h("pre", { class: "json-block", style: "white-space:pre-wrap" }, (s.commands || []).join("\n"))
          : h("div", { class: "muted" }, "未授权任何命令——本次诊断中该技能不会扩展只读命令白名单。"),
        h("div", { class: "muted", style: "margin-top:6px;font-size:0.76rem" },
          "只在加载了该技能的这次诊断内生效（会话结束即失效）；平台与目标机 Agent 双侧校验，"
          + "必须与命令完全一致才放行；危险命令黑名单（rm / shutdown / 管道进 shell 等）永远优先，不会被授权绕过。")),
      h("div", { class: "card" }, h("h3", {}, "效果（来自真实诊断流水）"),
        effectCell(s.effect),
        h("div", { class: "muted", style: "margin-top:6px;font-size:0.76rem" },
          "加载次数=用该技能诊断过的去重事件数；跟进率=这些诊断中人工处置为「跟进」的比例；"
          + "Token 中位数=这些诊断的模型消耗中位。参考指标（相关非因果）：技能常被用在更难的诊断上，跟进率天然偏高。")),
      h("div", { class: "card" }, h("h3", {}, "连接检查（fail-closed）"),
        (() => {
          const cc = s.connection_check || {};
          const reqs = ((cc.spec || {}).requires) || [];
          if (!reqs.length) return h("div", { class: "muted" }, "无外部依赖：核心方法论技能，恒可用。");
          return h("div", {},
            h("div", { class: "muted" }, `依赖项：${reqs.join("、")}　上次检查：${cc.checked_at ? fmtTime(cc.checked_at) : "未检查"}`),
            h("div", { class: cc.ok === false ? "note" : "muted", style: "margin-top:6px" },
              cc.ok === false
                ? `依赖未就绪——该技能不会进入索引、也无法加载（避免指向不存在的数据源）：${cc.note || ""}`
                : (cc.ok === true ? `连接正常${cc.note ? "：" + cc.note : ""}` : "尚未检查（默认放行；点列表里的「连接检查」执行）")));
        })()),
      h("div", { class: "card" }, h("h3", {}, "SKILL.md 正文"),
        // D127：默认渲染（正文含 ## 标题/列表/加粗），原文折叠备查——LLM 读的就是这份原文
        h("div", { class: "md-body", html: marked.parse(s.body_md || "") }),
        h("details", { class: "muted", style: "font-size:0.76rem;margin-top:8px" },
          h("summary", {}, "查看原文（Markdown 源码）"),
          h("pre", { class: "json-block", style: "white-space:pre-wrap;margin-top:6px" }, s.body_md || "（空）"))));
  }

  async function editor(r) {
    const s = r ? await api.get(`/api/ops/skills/${r.id}`) : {};
    const name = h("input", { class: "vs-input mono", value: s.name || "", placeholder: "my-skill" });
    const desc = h("input", { class: "vs-input", value: s.description || "", placeholder: "一句话说明何时用这个技能（必填）" });
    const prio = h("input", { class: "vs-input mono", type: "number", value: s.rca_priority ?? 10 });
    const scope = h("input", { class: "vs-input mono", value: (s.alerts_scope || []).join(", "),
      placeholder: "disk.*, 磁盘, inode（逗号分隔，留空=不自动注入）" });
    const cmds = h("textarea", { class: "vs-input mono", rows: "4", style: "height:auto;line-height:1.5" },
      (s.commands || []).join("\n"));
    const bodyMd = h("textarea", { class: "vs-input mono", rows: "14", style: "height:auto;line-height:1.55" },
      s.body_md || "## 何时用\n\n## 步骤\n1. \n\n## 产出\n");
    drawer(r ? `编辑技能 · ${s.name}` : "新建技能",
      h("div", { class: "note" }, "description 必填且要具体——它决定技能能否被正确匹配（Aurora 原话：质量决定匹配精度）。"),
      h("div", { class: "grid-2" },
        h("div", { class: "form-row" }, h("label", {}, reqLabel("名称")), name),
        h("div", { class: "form-row" }, h("label", {}, "优先级（越小越优先）", helpTip("越小越先注入。内置核心技能用 1，领域技能通常 5 起——避免领域技能抢在通用方法论之前。")), prio)),
      h("div", { class: "form-row" }, h("label", {}, reqLabel("描述")), desc),
      h("div", { class: "form-row" },
        h("label", {}, "适用告警模式", helpTip("自动路由范围。诊断发起时，事件的告警指标或规则名命中即自动加载该技能全文。\n"
          + "支持通配：cpu.*、*内存*、磁盘（规则名子串）。多个用逗号分隔。留空 = 不自动注入。")),
        scope),
      h("div", { class: "form-row" },
        h("label", {}, "授权命令（每行一条，只读）", helpTip("只在加载了该技能的这次诊断内临时放行这些只读命令。\n"
          + "必须与命令完全一致才放行；危险命令黑名单优先，技能无法绕过。")),
        cmds),
      h("div", { class: "form-row" }, h("label", {}, "正文（Markdown，四段式建议：何时用/步骤/产出/注意）"), bodyMd),
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        // D59：名称原先无校验（空名/超长 64 列直接撞库）；描述必填为既有语义
        const bad = needText(name.value, "技能名称", 64) || needText(desc.value, "描述", 512);
        if (bad) { toast(bad, "error"); return; }
        const scopeList = splitList(scope.value);
        const cmdList = splitLines(cmds.value);
        try {
          await busy(e.currentTarget, "保存中…", () => api.post("/api/ops/skills", { name: name.value.trim(), description: desc.value.trim(),
            body_md: bodyMd.value, rca_priority: Number(prio.value) || 10, enabled: true,
            alerts_scope: scopeList, commands: cmdList }));
          toast("已保存", "ok"); close(); draw();
        } catch (err) { toast(`保存失败：${err.message}`, "error"); }
      } }, "保存")));
  }

  function githubImporter() {
    const url = h("input", { class: "vs-input mono", placeholder: "https://github.com/owner/repo/tree/main/skills/xxx" });
    drawer("从 GitHub 导入技能",
      h("div", { class: "note" },
        "粘贴 GitHub 技能目录或 SKILL.md 文件链接（只支持 github.com / raw.githubusercontent.com）。"
        + "导入后会解析 frontmatter（name/description 必填），并把 alerts 与 commands 一并入库。"),
      h("div", { class: "form-row" }, h("label", {}, "GitHub 地址"), url),
      h("div", { class: "muted", style: "font-size:0.76rem" },
        "安全边界：仅允许上述两个域名、文件 ≤1MB、10 秒超时；技能自带的 scripts/ 目录不导入"
        + "（我们只收文本方法论与只读命令，不假装支持脚本执行环境）；同名技能会拒绝导入。"),
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        const u = (url.value || "").trim();
        if (!u) { toast("请填写 GitHub 地址", "error"); return; }
        try {
          const r = await busy(e.currentTarget, "导入中…", () => api.post("/api/ops/skills/import-github", { url: u }));
          toast(`已导入「${r.name}」${r.has_scripts ? "（含 scripts/ 目录说明，但未导入脚本）" : ""}`, "ok");
          close(); draw();
        } catch (err) { toast(`导入失败：${err.message}`, "error"); }
      } }, "导入")));
  }

  async function doImport(btn) {
    try {
      const r = await busy(btn || document.activeElement, "导入中…", () => api.post("/api/ops/skills/import"));
      toast(`内置同步 ${r.builtin_synced} 条；目录导入 ${r.imported} 条${r.failed.length ? `，失败 ${r.failed.length}` : ""}`, "ok");
      draw();
    } catch (e) { toast(`导入失败：${e.message}`, "error"); }
  }

  function splitList(v) {
    return String(v || "").split(/[,，\n]/).map((x) => x.trim()).filter(Boolean);
  }
  function splitLines(v) {
    return String(v || "").split("\n").map((x) => x.trim()).filter(Boolean);
  }
  function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v)); }
  const close = () => document.querySelector(".detail-head .btn")?.click();
  await draw();
}

export function destroy() {}
