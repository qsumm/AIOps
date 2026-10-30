// pages/copilot.js —— AI 助手 / ChatOps（FR-F ops-copilot；FR-D4）
// 必备：对话流（含 thinking / tool_call / tool_result 折叠渲染）、工具瀑布回放、输入框 + 历史会话侧栏
// D8 修正：①按服务端能力集判断可否对话，无权限时给出明确说明而不是"请求失败：HTTP 403"；
//          ②欢迎屏的示例问句从**真实主机名**生成，不再硬编码 demo-web-01（否则新装环境必然"未注册"）；
//          ③流式接口的 4xx 也读出后端 msg。
import { api, getUser, can, loadPerms } from "../api.js";
import { marked } from "/vendor/marked.esm.js";
// D86（实测确认）：这里用了 busy() 却没导入 —— 侧栏删除会话必报 `busy is not defined`、
// 会话删不掉且弹出"删除失败"。全站**只有本文件**漏了这个导入，已加机械守卫
// （test_page_modules_import_every_ui_helper_they_use）防止再犯。
import { h, toast, relTime, badge, svgIcon, esc, fmtTime, copyText, busy } from "../ui.js";

let es = null;
let currentSession = null;
let messages = [];      // [{role, content, tools:[{tool,params,preview,status}]}]
let ctxInfo = null;     // D87：最近一次 meta 带回的上下文用量（前端展示"记得多少"）
let foldedMark = 0;     // D87：重开会话时，前 N 条已被压缩为摘要

const md = (t) => { try { return marked.parse(t || "", { breaks: true, gfm: true }); } catch { return String(t || ""); } };

export async function render(root) {
  currentSession = null;
  messages = [];
  ctxInfo = null;
  foldedMark = 0;
  let welcomeSeq = 0;      // D49：欢迎屏序号——异步回来时若已离开欢迎屏，丢弃结果
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));
  await loadPerms().catch(() => {});

  const sList = h("div", { class: "chat-side" }, h("div", { class: "muted", style: "padding:8px 10px" }, "加载会话…"));
  const stream = h("div", { class: "chat-stream" });
  const input = h("textarea", { class: "vs-input chat-input", rows: "1",
    placeholder: "描述你的问题，或直接问某个指标…" });
  // 好用：Enter 发送、Shift+Enter 换行（聊天工具的标准约定）；高度随内容自适应
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 148) + "px";
  });
  const send = h("button", { class: "btn", onclick: () => ask() }, "发送");
  // D122：会话聚焦主机 —— 多选后"内存最高的进程"这类问题自动指向选中机器；不选=全局。
  let selHosts = [];          // 选中的 ident 列表
  let hostCatalog = [];       // 可选主机目录 [{ident,name,ip,status}]
  let hostPickOpen = false;
  const hostSelBar = h("div", { class: "chat-hosts" });
  /** D129：聚焦主机条精致化——把它从「一行散文字」改成有状态的卡片。
   *  - 未选：柔和底色 + 提示「作用于全平台」，与选中态视觉上明显分开
   *  - 已选：聚焦蓝边框 + 角标「N 台 · 优先围绕它们回答」+ chip（带状态点、IP、×）
   *  - 操作：右侧两个小按钮（清空 / 管理·选择主机），选用线性图标而非符号
   *  - 展开面板：标题行「已选 N/5」+ 每行状态点 + ✓ 勾选反馈 */
  function statusLabel(s) { return s === "up" ? "在线" : s === "down" ? "离线" : s === "pending" ? "待接入" : "未知"; }
  function renderHostSel() {
    const focused = selHosts.length > 0;
    hostSelBar.className = `chat-hosts${focused ? " has-focus" : ""}`;
    const chips = selHosts.map((ident) => {
      const h0 = hostCatalog.find((x) => x.ident === ident) || { name: ident.slice(0, 10), ip: "", status: "unknown" };
      const statusCls = h0.status === "up" ? "up" : h0.status === "down" ? "down" : h0.status === "pending" ? "pending" : "";
      return h("span", { class: "chat-host-chip" },
        h("span", { class: `chat-host-chip-status ${statusCls}`, title: statusLabel(h0.status) }),
        h("span", { class: "chat-host-chip-name" }, h0.name),
        h("span", { class: "chat-host-chip-ip" }, h0.ip || "-"),
        h("button", { class: "chat-host-chip-x", title: "移除", "aria-label": `移除 ${h0.name}`,
          onclick: () => { selHosts = selHosts.filter((x) => x !== ident); renderHostSel(); } },
          svgIcon("x", 11)));
    });
    hostSelBar.replaceChildren(
      h("span", { class: "chat-hosts-label" }, svgIcon("activity", 12), "聚焦主机"),
      h("span", { class: "chat-hosts-mode" },
        focused
          ? `${selHosts.length} 台 · 优先围绕它们回答`
          : "未选择 · 全平台对话"),
      focused
        ? h("div", { class: "chat-hosts-chips" }, ...chips)
        : h("div", { class: "chat-hosts-empty" }, svgIcon("info", 11),
            "助手会基于全部在线主机回答；想精确指向某些机器，点右侧「选择主机」"),
      h("div", { class: "chat-hosts-actions" },
        focused
          ? h("button", { class: "chat-hosts-toggle", title: "清空所有聚焦主机",
              onclick: () => { selHosts = []; hostPickOpen = false; renderHostSel(); } }, "清空")
          : null,
        h("button", { class: "chat-hosts-toggle",
          title: "从在线主机里挑（可多选 1~5 台）",
          onclick: () => { hostPickOpen = !hostPickOpen; renderHostSel(); } },
          hostPickOpen ? svgIcon("x", 11) : svgIcon("activity", 11),
          h("span", {}, hostPickOpen ? "收起" : (focused ? "管理" : "选择主机")))));
    if (hostPickOpen) {
      hostSelBar.append(h("div", { class: "chat-host-pick" },
        h("div", { class: "chat-host-pick-head" },
          h("span", {}, "勾选 1~5 台（仅在线主机）"),
          h("span", { class: "mono" }, `${selHosts.length} / 5`)),
        ...hostCatalog.filter((x) => x.status === "up").map((h0) => {
          const picked = selHosts.includes(h0.ident);
          return h("div", { class: "chat-host-row" + (picked ? " picked" : ""),
            onclick: () => {
              if (picked) selHosts = selHosts.filter((x) => x !== h0.ident);
              else if (selHosts.length >= 5) { toast("最多聚焦 5 台主机", "warn"); return; }
              else selHosts.push(h0.ident);
              renderHostSel();
            } },
            h("span", { class: "chat-host-chip-status up", title: statusLabel(h0.status) }),
            h("span", { class: "chat-host-row-name" }, h0.name),
            h("span", { class: "chat-host-row-ip" }, h0.ip || "-"),
            h("span", { class: "picked-mark", style: picked ? "color:var(--accent);font-weight:600" : "color:var(--muted)" }, picked ? "✓" : ""));
        })));
    }
  }
  api.get("/api/ops/hosts").then((d) => {
    hostCatalog = d.items || [];
    renderHostSel();
  }).catch(() => {});

  // D87：上下文用量条 —— 让用户看得见"助手记得多少"。长会话会被自动压缩（滚动摘要 + 最近窗口），
  // 以前用户完全无从得知，只能感觉"它怎么忘了"。这里明说：窗口内几条、已压缩几条、约占多少预算。
  const ctxBar = h("div", { class: "chat-ctx muted" });
  function renderCtx() {
    const c = ctxInfo;
    if (!c) { ctxBar.replaceChildren(); return; }
    const parts = [`本轮纳入最近 ${c.kept} 条对话`];
    if (c.folded_total) parts.push(`更早 ${c.folded_total} 条已压缩为摘要`);
    if (c.tokens) parts.push(`约占 ${Math.round(c.tokens / 100) / 10}k / ${Math.round(c.budget / 1000)}k token`);
    ctxBar.replaceChildren(
      svgIcon("info", 11),
      h("span", {}, parts.join(" · ")),
      ...(c.compacted ? [h("span", { class: "chat-ctx-tag" }, "本轮已压缩")] : []));
  }

  // 角色无权使用 ChatOps（如关闭了演示模式的 demo 账号）：把话说清楚，而不是给一个必然 403 的输入框
  if (!can("chat")) {
    root.replaceChildren(h("div", { class: "chat-wrap" },
      h("div", { class: "chat-main" },
        h("div", { class: "chat-head" },
          h("div", {},             h("div", { class: "muted chat-sub" }, "只读问答：可查指标 / 探活 / 主机信息，不会执行任何变更"))),
        h("div", { class: "chat-welcome" },
          h("div", { class: "chat-welcome-title" }, "当前账号无权使用 AI 助手"),
          h("div", { class: "muted" },
            `当前角色「${(getUser() || {}).role || "未知"}」只能浏览数据。AI 助手会调用大模型（按用量计费），`
            + "因此限管理员与普通成员使用；演示账号是否开放由平台管理员决定。"),
          h("div", { class: "muted" }, "需要排查问题时，可到「问题看板」查看诊断报告，或联系管理员开通权限。")))));
    return;
  }

  root.replaceChildren(
    h("div", { class: "chat-wrap" },
      sList,
      h("div", { class: "chat-main" },
        h("div", { class: "chat-head" },
          h("div", {}, h("div", { class: "muted chat-sub" }, "只读问答：可查指标 / 探活 / 主机信息，不会执行任何变更")),
          h("button", { class: "btn ghost chat-cmp", title: "把更早的对话折叠成摘要，给最近内容腾地方（超长会话才需要）", onclick: () => compactSession() }, "压缩历史"),
          h("button", { class: "btn", onclick: () => newSession() }, "+ 新会话"),
          h("button", { class: "btn ghost", onclick: () => clearChat() }, "清空对话")),
        stream,
        hostSelBar,
        h("div", { class: "chat-input-row" }, input, send),
        ctxBar,
        h("div", { class: "chat-input-hint muted" },
          svgIcon("info", 11),
          h("span", {}, "Enter 发送 · Shift+Enter 换行 · 助手仅做只读查询，不会变更任何配置")))));

  welcome();
  await loadSessions();
  // 注意：不要在此调用 redraw()——messages 为空时它会用空列表覆盖掉欢迎屏（D4 实测）

  async function welcome() {
    // D47：推荐问题由**后端结合实时状态 + 大模型**生成（不再是写死的通用题库）。
    // D49：改为后端**定时预生成 + 缓存**——本函数只读缓存（毫秒级），不再让用户等模型；
    //      缓存为空（服务刚重启）时后端先给规则版并后台补生成，12 秒后这里回来取一次模型版。
    const seq = ++welcomeSeq;
    let sug = { headline: "我能帮你查数据、说清现象", suggestions: [], source: "rule" };
    try { sug = await api.get("/api/ops/chat/suggestions"); } catch { /* 降级见下 */ }
    if (seq !== welcomeSeq || messages.length) return;      // 已开始对话/切换页面：别覆盖对话流
    renderSug(sug);
    if (sug.source === "rule") {
      setTimeout(async () => {
        if (seq !== welcomeSeq || messages.length || currentSession) return;
        try {
          const s2 = await api.get("/api/ops/chat/suggestions");
          if (s2.source !== "rule" && seq === welcomeSeq && !messages.length) renderSug(s2);
        } catch { /* 保持规则版 */ }
      }, 12000);
    }
  }

  function renderSug(sug) {
    const list = (sug.suggestions || []).filter((x) => x && x.q);
    const btn = h("button", { class: "btn ghost chat-sug-refresh", title: "按当前状态立即更新推荐问题",
      onclick: () => refreshSug(btn) }, svgIcon("refresh", 12), h("span", {}, "刷新"));
    stream.replaceChildren(h("div", { class: "chat-welcome" },
      h("div", { class: "chat-sug-head" },
        h("div", { class: "chat-welcome-title" }, sug.headline || "我能帮你查数据、说清现象"),
        h("div", { class: "chat-sug-tools" },
          sug.generated_at ? h("span", { class: "muted chat-sug-time" }, `更新于 ${fmtTime(sug.generated_at).slice(11, 16)}`) : null,
          btn)),
      h("div", { class: "chat-welcome-sub muted" }, "点下面任一条直接提问，或在下方输入自己的问题。助手只做只读查询，不会变更任何东西。"),
      h("div", { class: "chat-sugs" }, ...(list.length ? list : [
        { q: "当前整体状况怎么样？", why: "", tone: "ok" },
      ]).map((x) => h("button", { class: `chat-sug tone-${x.tone || "info"}`,
          onclick: () => { input.value = x.q; ask(); } },
          h("span", { class: "chat-sug-q" }, x.q),
          x.why ? h("span", { class: "chat-sug-why" }, x.why) : null)))));
  }

  // 手动刷新（D49）：立即调一次模型（后端 30s 冷却），完成后原地替换列表
  async function refreshSug(btn) {
    btn.disabled = true;
    btn.classList.add("loading");                  // 线性图标原地转圈（比换文字更清楚）
    const label = btn.querySelector("span:not(.ico)");   // 注意：第一个 span 是图标，不能选它
    const old = label ? label.textContent : "";
    if (label) label.textContent = "更新中…";
    try {
      const s = await api.post("/api/ops/chat/suggestions/refresh");
      welcomeSeq++;                                  // 让可能挂着的"12 秒补取"失效
      renderSug(s);
      toast(s.cooldown ? "刚刚更新过，请稍候再试" : "已按当前状态更新推荐问题");
    } catch (e) {
      toast(e.message || "更新失败，请稍后再试", "error");
      btn.disabled = false;
      btn.classList.remove("loading");
      if (label) label.textContent = old;
    }
  }

  /** D55：清空当前对话内容（回到欢迎屏；历史会话仍可在左侧找回）。 */
  function clearChat() {
    messages = [];
    if (es) { es.close(); es = null; }
    streaming = false;
    currentSession = null;
    selHosts = []; hostPickOpen = false; renderHostSel();
    ctxInfo = null; foldedMark = 0; renderCtx();
    welcome();
    toast("已清空当前对话（历史会话仍在左侧）", "ok");
  }

  /** D88：手动压缩当前会话历史。只动自己名下的会话（后端同款会话隔离）。 */
  async function compactSession() {
    if (!currentSession) { toast("请先在当前会话发几条消息，再压缩历史", "warn"); return; }
    const btn = document.querySelector('.chat-cmp');
    try {
      const d = await busy(btn, "压缩中…", () => api.post(`/api/ops/chat/sessions/${currentSession}/compact`));
      if (!d || !d.compacted) { toast((d && d.note) || "没有需要折叠的历史，点击可稍后再试", "info"); return; }
      // 压缩后务必刷新会话（让折叠标记与 meta 与后端一致）
      foldedMark = d.folded || d.summary_upto || 0;
      await openSession(currentSession);
      toast(d.note || "已压缩更早的历史为摘要", "ok");
    } catch (e) { toast(`压缩失败：${e.message}`, "error"); }
  }

  function newSession() {
    currentSession = null; messages = [];
    selHosts = []; hostPickOpen = false; renderHostSel();
    ctxInfo = null; foldedMark = 0; renderCtx();
    if (es) { es.close(); es = null; }
    welcome();
    loadSessions();
  }

  async function loadSessions() {
    try {
      const d = await api.get("/api/ops/chat/sessions");
      const items = d.items || [];
      sList.replaceChildren(
        h("div", { class: "chat-side-title muted" }, `历史会话（${items.length}）`),
        ...(items.length ? items.map((s) => h("div", { class: "chat-sess" + (s.id === currentSession ? " active" : ""), onclick: () => openSession(s.id) },
          h("div", { class: "chat-sess-title" }, s.title),
          h("div", { class: "muted", style: "font-size:0.72rem" },
            `${s.messages} 条${s.folded ? ` · 已压缩 ${s.folded}` : ""} · ${relTime(s.updated_at)}`),
          h("button", { class: "btn ghost chat-sess-del", onclick: async (e) => {
            e.stopPropagation();
            try { await busy(e.currentTarget, "…", () => api.del(`/api/ops/chat/sessions/${s.id}`)); } catch (err) { toast(`删除失败：${err.message}`, "error"); }
            if (s.id === currentSession) newSession(); else loadSessions(); } }, "✕")))
          : [h("div", { class: "muted", style: "padding:8px 10px;font-size:0.8rem" }, "暂无历史")]));
    } catch { /* ignore */ }
  }

  async function openSession(id) {
    const s = await api.get(`/api/ops/chat/sessions/${id}`);
    currentSession = s.id;
    selHosts = s.target_hosts || []; hostPickOpen = false; renderHostSel();
    messages = (s.messages || []).filter((m) => m.role !== "system");
    // D87：重开会话也要如实显示"更早 N 条已压缩"——与后端发给模型的窗口口径一致
    foldedMark = s.summary_upto || 0;
    ctxInfo = foldedMark ? { kept: messages.length - foldedMark, folded_total: foldedMark, tokens: 0, budget: 0, compacted: false } : null;
    renderCtx();
    // D148：空会话不能只留一片白——明确告诉用户"这里为什么没东西"。
    // （0 条会话已不再出现在历史列表；这里兜住「打开期间刚好被清空」等边缘情况。）
    if (!messages.length) {
      stream.replaceChildren(h("div", { class: "chat-welcome" },
        h("div", { class: "chat-welcome-title" }, "这段会话没有消息"),
        h("div", { class: "muted", style: "margin-top:6px" },
          "可能是上一次回答生成中断。直接在下方输入问题即可继续对话。")));
    } else {
      redraw();
    }
    loadSessions();
  }

  // 工具调用摘要（可信性核心）：把"助手查了什么证据"讲清楚 —— 工具名 / 参数 / 返回摘要
  function toolBlock(tools) {
    return h("details", { class: "msg-tools" },
      h("summary", {},
        svgIcon("activity", 12),
        h("span", {}, `本次查阅了 ${tools.length} 项数据`),
        h("span", { class: "msg-tools-hint muted" }, "（展开看明细）")),
      ...tools.map((t) => h("div", { class: "tool-line" },
        h("div", { class: "tool-head" },
          h("span", { class: "tool-name mono" }, t.tool),
          t.params && Object.keys(t.params).length
            ? h("span", { class: "tool-args mono muted" }, JSON.stringify(t.params).slice(0, 80)) : null),
        t.preview ? h("div", { class: "tool-preview muted" }, t.preview.slice(0, 220)) : null)));
  }

  // 每条消息带角色与时间（可信：知道谁在什么时候说的）
  function msgMeta(role, ts) {
    return h("div", { class: "msg-meta" },
      h("span", { class: "msg-who" }, role === "user" ? "你" : "AI 助手"),
      ts ? h("span", { class: "msg-time muted" }, fmtTime(ts)) : null);
  }

  let streaming = false;   // D54：回答生成中（含出字后）→ 最后一条助手消息带转圈中间态
  function redraw() {
    const box = h("div", { class: "chat-list" });
    // D87：被压缩掉的更早对话在流里留一条标记 —— 用户能看到"这段被折叠了"，
    // 而不是发现历史突然"少了"（诚实边界：摘要是有损的，明说比藏着好）
    if (foldedMark > 0) {
      box.append(h("div", { class: "chat-folded" },
        svgIcon("info", 11),
        h("span", {}, `更早的 ${foldedMark} 条对话已压缩为摘要（助手仍记得其中的结论与约束，但细节有损）`)));
    }
    for (const m of messages) {
      const isUser = m.role === "user";
      const isLast = m === messages[messages.length - 1];
      const body = h("div", { class: "msg-body", html: isUser ? esc(m.content) : md(m.content) });
      // D54：生成中 → 转圈 + 状态词（出字前"思考中…"，出字后"回答中…"）；结束由 finally 的 redraw 摘除
      if (streaming && !isUser && isLast) {
        body.append(h("span", { class: "chat-typing" },
          svgIcon("refresh", 12), h("span", {}, m.content ? "回答中…" : "思考中…")));
      }
      // D55：助手回答可复制（用户要求）——按钮挂在气泡下方（悬停显示，不占布局），
      // 不放 meta 行：窄气泡会把"你/时间"挤出去（实测）。复制原始 Markdown，贴到别处不丢格式。
      const copyBtn = (!isUser && m.content && !(streaming && isLast))
        ? h("button", { class: "btn ghost msg-copy", title: "复制本条回答",
            onclick: () => { copyText(m.content); } }, svgIcon("refresh", 11) && "复制")
        : null;
      box.append(h("div", { class: `msg-row ${isUser ? "from-user" : "from-agent"}` },
        h("div", { class: `msg ${isUser ? "user" : "agent"}` },
          msgMeta(m.role, m.ts),
          m.tools?.length ? toolBlock(m.tools) : null,
          body,
          copyBtn)));
    }
    stream.replaceChildren(box);
    stream.scrollTop = stream.scrollHeight;
  }

  async function ask() {
    // D64（并发守卫）：回答流式输出中禁止再发——输入框此时并未锁定，用户打完下一问回车
    // 会并发开第二条 SSE 流，两条流的增量互相覆盖消息（真实用户"急性子"场景）。
    if (streaming) { toast("回答生成中，请稍候…", "warn"); return; }
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    send.disabled = true;
    messages.push({ role: "user", content: text, ts: new Date().toISOString() });
    const placeholder = { role: "assistant", content: "", tools: [], thinking: "" };
    messages.push(placeholder);
    streaming = true;
    redraw();

    const live = h("div", { class: "live-log" });
    stream.querySelector(".msg:last-child .msg-body")?.append(live);

    // EventSource 只能 GET，而对话需要 POST 消息体 → 用 fetch 读 SSE 流（带 Bearer，无需 ticket）
    try {
      const resp = await fetch("/api/ops/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("ops_token")}` },
        body: JSON.stringify({ message: text, session_id: currentSession, target_hosts: selHosts }),
      });
      if (!resp.ok || !resp.body) {
        // 后端错误统一为 {code,msg,data}（D8）→ 读出人话原因，不再只显示 "HTTP 403"
        let reason = `HTTP ${resp.status}`;
        try {
          const j = await resp.json();
          if (j && j.msg) reason = j.msg;
        } catch { /* 非 JSON 响应 */ }
        if (resp.status === 403) {
          placeholder.content = `> ${reason}`;
          toast(reason, "warn");
          return;   // 交给 finally 收尾
        }
        throw new Error(reason);
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop();
        for (const chunk of parts) {
          const ev = /^event: (\w+)/m.exec(chunk)?.[1];
          const dataRaw = /^data: (.*)$/m.exec(chunk)?.[1];
          if (!ev || !dataRaw) continue;
          const p = JSON.parse(dataRaw);
          if (ev === "tool_call") { placeholder.tools.push({ tool: p.tool, params: p.params, preview: "" }); }
          else if (ev === "tool_result") { const last = placeholder.tools[placeholder.tools.length - 1]; if (last) { last.preview = p.preview; } }
          else if (ev === "token") { placeholder.content += p.text; }
          else if (ev === "error") { placeholder.content += `\n\n> ${p.msg}`; }
          else if (ev === "meta") {
            currentSession = p.session_id;
            if (p.context) { ctxInfo = p.context; foldedMark = p.context.folded_total || 0; renderCtx(); }
          }
          redraw();
        }
      }
    } catch (e) {
      placeholder.content = `请求失败：${e.message}`;
      toast(`对话失败：${e.message}`, "error");
    } finally {
      streaming = false;   // D54：摘除转圈（成功/失败都走这里）
      send.disabled = false;
      redraw();
      loadSessions();
    }
  }
}

export function destroy() { if (es) { es.close(); es = null; } }
