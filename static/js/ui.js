// ui.js —— 通用 UI 原语（表格 / 徽章 / 抽屉 / toast / 图表），内部 UI 规范 §5 同构

import { api } from "./api.js";   // D40：hostLabel 需要拉主机列表建 ident→name 映射

export const h = (tag, attrs = {}, ...children) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v);
  }
  children.flat().forEach((c) => {
    if (c === null || c === undefined || c === false) return;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  });
  return el;
};
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// 状态 → 语义四色徽章（规范 §9 状态映射）
// resolved → SKIP（D23）：绿=语义"健康/通过"（up/recovered/ok），"已结束"一律灰。
const BADGE = { up: "UP", down: "DOWN", firing: "FAIL", ack: "WARN", resolved: "SKIP",
  recovered: "PASS", pending: "PENDING", ok: "UP", failed: "FAIL", unknown: "SKIP",
  sent: "PASS", running: "RUNNING", succeeded: "PASS", success: "PASS", timeout: "WARN",
  open: "PROGRESS", done: "PASS", executing: "RUNNING" };
// D23 配色降噪（P5）：S3 是"通知"不是"注意"——蓝只留给可交互元素（规范 §11 随动口径），
// 改用 skip 灰；红/青蓝仍归 S1/S2（警告色历经琥珀→紫罗兰→青蓝，D53 定青蓝；暖色语义仍禁用）。
export const SEV = {
  1: ["S1 紧急", "FAIL", "S1 紧急：最严重，需立即处理（级别由告警规则定义）"],
  2: ["S2 警告", "WARN", "S2 警告：影响明显，尽快处理（级别由告警规则定义）"],
  3: ["S3 通知", "SKIP", "S3 通知：记录与观察，不需要夜间响应（级别由告警规则定义）"],
};

/** 行内标记：先整体转义 HTML，再把 **加粗** / `代码` 变成元素。
 *  为什么需要：页面里的"提醒卡片"文案常带强调（如"平台**不代执行**"），
 *  直接塞进 textContent 会把星号原样显示出来（实测观感问题），
 *  而用 innerHTML 又不安全 —— 所以统一走这个"先转义再标记"的出口。 */
export const rich = (text) => esc(text).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
  .replace(/`([^`]+)`/g, "<code>$1</code>");

/** 提示卡片（note）：统一承载说明/警告文案，支持上面那套行内标记。 */
export function note(text, cls = "note") {
  return h("div", { class: cls, html: rich(text) });
}

/** 悬浮问号（helpTip）：给"专业但不好懂"的控件补一句人话说明。
 *
 *  为什么要它（用户要求降低学习成本）：平台上有些控件是**领域判断**而不是名字能自解释的
 *  （执行方式三档、通知节奏、探活类型、置信度、连接检查…）。给它们一个 ? 悬浮说明，
 *  比塞进页面正文更省地方、也比让人猜更负责。
 *
 *  不加什么（同一条要求）：删除 / 关闭 / 刷新 / 详情这类**自明操作不加**——问号泛滥反而变成噪音。
 *
 *  交互与布局：
 *   · hover / 键盘聚焦（tabindex）都能看到；移动端点击也能看（没有 hover）；
 *   · 气采用**共享单例 + fixed 定位**并按视口夹紧：下方空间不够自动翻到上方，
 *     左右夹紧不越界——所以在抽屉（z-index 901）与表格里都不会被裁切或撑破布局；
 *   · 默认 display:none，**静态不占位、不遮挡任何东西**；滚动/缩放即收起（避免与内容错位）。
 */
let _htipNode = null;
let _htipCleanupBound = false;

function _htipHide() {
  if (_htipNode) _htipNode.classList.remove("on");
}

function _htipShow(anchor, text) {
  if (!_htipNode) {
    _htipNode = h("div", { class: "htip-bubble", role: "tooltip" });
    document.body.appendChild(_htipNode);
  }
  const b = _htipNode;
  // 用 rich() 渲染：说明里常有 **强调**（先整体转义再变元素，安全）。直接 textContent 会把星号露出来。
  b.innerHTML = rich(text);
  b.style.left = "0px";
  b.style.top = "0px";
  b.classList.add("on");                 // 先显示才能量到尺寸
  const a = anchor.getBoundingClientRect();
  const br = b.getBoundingClientRect();
  const pad = 8;
  let left = a.left + a.width / 2 - br.width / 2;
  left = Math.max(pad, Math.min(left, window.innerWidth - br.width - pad));
  let top = a.bottom + 8;
  if (top + br.height > window.innerHeight - pad) top = Math.max(pad, a.top - br.height - 8);
  b.style.left = `${Math.round(left)}px`;
  b.style.top = `${Math.round(top)}px`;
  if (!_htipCleanupBound) {
    _htipCleanupBound = true;
    window.addEventListener("scroll", _htipHide, true);
    window.addEventListener("resize", _htipHide);
  }
}

export function thLabel(text, tip) { return h("span", {}, text, " ", helpTip(tip)); }

export function helpTip(text) {
  const el = h("span", { class: "htip", tabindex: "0", role: "note", "aria-label": `说明：${text}` }, "?");
  const show = () => _htipShow(el, text);
  el.addEventListener("mouseenter", show);
  el.addEventListener("focus", show);
  el.addEventListener("mouseleave", _htipHide);
  el.addEventListener("blur", _htipHide);
  el.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); show(); });
  return el;
}

/** 填充容器子节点，自动丢弃 null/undefined/false/空串。
 *
 *  为什么需要：h() 会过滤假值，但 Element.replaceChildren() **不会** ——
 *  它按 DOM 规范把 null 转成文本节点，页面里就凭空出现一行 "null"
 *  （P5 终验在恢复结果面板上实测到：`r.error ? ... : null` 直接印出了 null）。
 *  条件子节点一律改用本函数，别再手写 replaceChildren(... ? x : null)。
 */
export function fill(el, ...kids) {
  el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false && k !== ""));
  return el;
}

export function badge(text, cls) { return h("span", { class: `badge ${cls || BADGE[text] || ""}` }, text); }
export function sevBadge(sev) { const [t, c, tip] = SEV[sev] || ["S3 通知", "SKIP", "S3 通知：记录与观察"]; return h("span", { class: `badge ${c}`, title: tip }, t); }
// D61：状态必须说人话——此前表格里直接渲染英文枚举（用户截图：告警列表一列 "ack"/"firing"，
// 顶栏一个 "up"），非英文用户看不懂。徽章颜色沿用语义映射，文字换中文。
const BADGE_TEXT = {
  up: ["在线", "在线：最近心跳正常（超过 45 秒收不到心跳即判离线）"],
  down: ["离线", "离线：超过 45 秒未收到 Agent 心跳（主机记录与历史数据保留）"],
  firing: ["告警中", "告警中：问题正在发生，平台持续监测、尚未恢复"],
  ack: ["已确认", "已确认：有人认领处理中，尚未恢复"],
  resolved: ["已解决", "已解决"], recovered: ["已恢复", "已恢复：指标回到阈值内"],
  pending: ["等待中", "等待中：已创建、尚未开始执行"], ok: ["正常", "正常"], failed: ["失败", "失败：最近一次执行/测试未成功（点击查看错误详情）"],
  unknown: ["未测", "未测：还没有执行过测试"], sent: ["已发送", "已发送"], running: ["进行中", "进行中"],
  succeeded: ["成功", "成功"], success: ["成功", "成功"], timeout: ["超时", "超时：等待超过时限，稍后自动重试"],
  idle: ["未运行", "未运行：任务已创建但未到执行时间"],
  executed: ["已执行", "已执行"], approved: ["已批准", "已批准：人工确认通过"],
  draft: ["草稿", "草稿：已定义、尚未执行过"], aborted: ["已中止", "已中止：执行前被人工取消"],
  open: ["进行中", "进行中"], done: ["已完成", "已完成"], executing: ["执行中", "执行中"],
};
export function statusBadge(s, cls) {
  const key = String(s ?? "");
  const [text, tip] = BADGE_TEXT[key] || [key, `状态：${key}`];
  return h("span", { class: `badge ${cls || BADGE[key] || ""}`, title: tip }, text);
}

/* ─────────── D51：耗时操作的统一"中间态" ───────────
   用户要求：「涉及到中间态的，比如生成报告，这个等待的过程要有中间态，全局检查」。
   为什么必须有：等待期间没有任何反馈 = 用户不知道"在跑还是死了" → 重复点击
   （实测：巡检「立即执行」原来只有一条 3.2s 就消失的 toast，按钮还能再点，可能并发跑两次巡检；
   「测试」大模型连通要 10~20s，期间按钮毫无变化）。

   busy(btn, text, fn)：按钮级中间态
     ① 按钮禁用 + 线性图标原地转圈 + 文案换成进行中文案（"试运行中…"）；
     ② 超过 2 秒才追加已用秒数（"生成中… 12s"）——短操作不闪，长操作能确认"没死"；
     ③ 失败时自动恢复原状并把异常抛回调用方（由调用方 toast）；
     ④ 返回 fn() 的结果。
   实现注记：只动文本节点，**不重建 innerHTML** —— h() 用 addEventListener 挂 onclick，
   重建 innerHTML 会把监听器一起丢掉（这一点在别处踩过）。 */
export async function busy(btn, text, fn) {
  const el = typeof btn === "string" ? document.getElementById(btn) : btn;
  const prevHtml = el ? el.innerHTML : "";
  const spin = el ? svgIcon("refresh", 12) : null;
  if (el) {
    el.disabled = true;                    // button 生效；div/card 由 .busy 的 pointer-events 兜住
    el.classList.add("busy");
    el.textContent = "";
    if (spin) el.append(spin);
    el.append(document.createTextNode(text));
  }
  const t0 = Date.now();
  const timer = el ? setInterval(() => {
    const sec = Math.round((Date.now() - t0) / 1000);
    if (sec >= 2 && el.lastChild) el.lastChild.nodeValue = `${text} ${sec}s`;
  }, 1000) : null;
  try {
    return await fn();
  } finally {
    if (timer) clearInterval(timer);
    if (el) { el.disabled = false; el.classList.remove("busy"); el.innerHTML = prevHtml; }
  }
}

/** 区块级"进行中"行（比按钮显眼，用于长任务：巡检、批量诊断）。
    container.prepend 一行转圈 + 文案 + 已用时间；返回 { done(msg), fail(msg) } 收尾。 */
export function progress(container, text) {
  const line = h("div", { class: "task-progress" }, svgIcon("refresh", 13), h("span", {}, text));
  container.prepend(line);
  const t0 = Date.now();
  const timer = setInterval(() => {
    line.replaceChildren(svgIcon("refresh", 13),
      h("span", {}, `${text} · 已用 ${Math.round((Date.now() - t0) / 1000)}s`));
  }, 1000);
  const finish = (msg, cls) => {
    clearInterval(timer);
    line.classList.add(cls);
    line.replaceChildren(svgIcon(cls === "ok" ? "check" : "alert", 13), h("span", {}, msg));
    setTimeout(() => line.remove(), 6000);
  };
  return { done: (m) => finish(m, "ok"), fail: (m) => finish(m, "fail") };
}

export function toast(msg, kind = "info", ms = 0) {
  let box = document.getElementById("toast-box");
  if (!box) { box = h("div", { id: "toast-box" }); document.body.append(box); }
  const t = h("div", { class: `toast ${kind}` }, msg);
  box.append(t);
  // D175-2（用户反馈："提示弹窗消失的太快了，起码等待 6s"）：原 3.2s 太短——
  // 我们的提示大多是「一句话结论 + 下一步动作」（如"报告里没有已验证的修复建议段…"），
  // 用户刚读完、想照着做，提示已经没了。下限 6s，长文案按字数再加时间；
  // 另加「点击即关」，长提示不必干等。
  const text = String(msg || "");
  const life = ms > 0 ? Math.max(ms, 6000) : Math.max(6000, 2600 + text.length * 90);
  t.title = "点击可关闭";
  t.style.cursor = "pointer";
  t.addEventListener("click", () => t.remove());
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, life);
}

/* 主机 ident → 主机名 / IP 映射（D40）。
   后端多处只返回 host_ident（机器指纹哈希前 32 位），前端直接截前 10 位显示 →
   用户看到 "391877d6656a" 完全不知道是哪台机器（用户报障）。
   这里加载一次主机列表建映射，各处用 hostLabel() 显示**主机名**；
   未命中（主机已删除/无权限）回退到短 ident。 */
let _hostNames = null;
let _hostIps = null;
let _hostNamesPromise = null;

export async function ensureHostNames() {
  if (_hostNames) return _hostNames;
  if (!_hostNamesPromise) {
    _hostNamesPromise = (async () => {
      try {
        const d = await api.get("/api/ops/hosts");
        const m = {}, mi = {};
        (d.items || []).forEach((h) => { m[h.ident] = h.name || h.ident.slice(0, 8); mi[h.ident] = h.ip || ""; });
        _hostNames = m; _hostIps = mi;
      } catch { _hostNames = {}; _hostIps = {}; }
      return _hostNames;
    })();
  }
  return _hostNamesPromise;
}

/** 取主机显示名：优先主机名，未命中回退短 ident。需先 await ensureHostNames()。 */
export function hostLabel(ident) {
  if (!ident) return "-";
  const n = _hostNames && _hostNames[ident];
  return n || ident.slice(0, 10);
}

/** 取主机 IP：优先主机上报 ip，未命中回空串。需先 await ensureHostNames()。 */
export function hostIp(ident) {
  return (ident && _hostIps && _hostIps[ident]) || "";
}

/* 接入前置条件卡片（D35）：显式列出目标机需满足的条件，避免"环境不满足 → 装失败"
   时用户无从排查。条目与 agent/install.sh 的实际行为一一对应。 */
export function precheckCard(origin) {
  const items = [
    ["操作系统", "Linux（systemd）或 macOS（launchd）"],
    ["Python", '3.9+。Linux 若只有旧版，用 dnf/apt 装 python3.11；macOS 用系统自带 <code>/usr/bin/python3</code>'],
    ["权限", "Linux 需 root 或 sudo（写 systemd 单元）；macOS 当前用户即可"],
    ["网络", `目标机需能出站访问 ${origin}；无需在目标机开放任何入站端口`],
    ["依赖", "psutil / httpx 由安装脚本自动安装"],
  ];
  return h("div", { class: "card precheck" },
    h("h3", {}, "前置条件（先确认，否则可能装失败）"),
    h("div", { class: "precheck-list" }, ...items.map(([k, v]) =>
      h("div", { class: "precheck-row" },
        h("span", { class: "precheck-k" }, k),
        h("span", { class: "precheck-v", html: v })))));
}

/* 复制文本到剪贴板（D28）。
   为什么不能直接 `navigator.clipboard.writeText`：该 API **只在安全上下文**
   （HTTPS 或 localhost）存在。平台常以 `http://<公网IP>:<端口>` 部署，此时
   `navigator.clipboard` 是 undefined，直接调用会抛
   "Cannot read properties of undefined (reading 'writeText')"——本地
   （127.0.0.1 属安全上下文）测不出来，部署到 HTTP 公网才暴露。
   降级链：Clipboard API → execCommand('copy')（HTTP 下仍可用）→ 提示手动复制。 */
export function copyText(text, onOk, onFail) {
  const done = (ok) => {
    if (ok) { onOk ? onOk() : toast("已复制", "ok"); }
    else { onFail ? onFail() : toast("浏览器限制无法自动复制，请手动选中文本复制", "warn"); }
  };
  try {
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => done(true)).catch(() => done(_execCopy(text)));
      return;
    }
  } catch { /* 落到降级 */ }
  done(_execCopy(text));
}

function _execCopy(text) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch { return false; }
}

// 右侧详情抽屉（规范 §5 .detail-panel）
let _drawerKeyHandler = null;   // Esc 关抽屉的监听（打开时挂、关闭时摘，避免叠加）
export function drawer(title, ...content) {
  // 接受两种调用：drawer("title", ...content) 或 drawer("title", {width: 720}, ...content) —— 第二参为对象时作 opts
  let opts = {};
  let body = content;
  if (content.length && content[0] && typeof content[0] === "object" && !content[0].tagName && !content[0].nodeName) {
    opts = content.shift() || {};
    body = content;
  }
  const maxW = opts.width || 1080;   // D157：从默认 720 提到 1080（高信息密度表单更舒服，E2E 守卫 ≥1000px 守底）   // D157：从默认 560 提到 720（信息密度高的表单更舒服）
  closeDrawer();
  const onkey = (e) => { if (e.key === "Escape") closeDrawer(); };
  document.addEventListener("keydown", onkey);
  _drawerKeyHandler = onkey;
  const panel = h("aside", { class: "detail-panel", style: `max-width:${maxW}px` },
    h("div", { class: "detail-head" }, h("h3", {}, title), h("button", { class: "btn ghost", onclick: closeDrawer }, "✕")),
    h("div", { class: "detail-body" }, ...body));
  const mask = h("div", { class: "drawer-mask", onclick: closeDrawer });
  document.body.append(mask, panel);
  requestAnimationFrame(() => { panel.classList.add("open"); mask.classList.add("open"); });
  return panel;
}
export function closeDrawer() {
  if (_drawerKeyHandler) { document.removeEventListener("keydown", _drawerKeyHandler); _drawerKeyHandler = null; }
  document.querySelectorAll(".detail-panel, .drawer-mask").forEach((e) => e.remove());
}

// 表格：columns=[{key,title,width,render(row, index)}]（index 从 0 起，供序号列用）
export function table(columns, rows, opts = {}) {
  if (!rows || !rows.length) {
    // D166：支持自定义空态渲染（图标 + 引导按钮），纯文本 emptyText 仍兼容
    if (typeof opts.emptyRender === "function") return opts.emptyRender();
    return h("div", { class: "empty" }, opts.emptyText || "暂无数据");
  }
  const thead = h("thead", {}, h("tr", {}, ...columns.map((c) => h("th", { style: c.width ? `width:${c.width}` : "" }, c.title))));
  const tbody = h("tbody", {}, ...rows.map((r, ri) => h("tr", { class: opts.onRowClick ? "clickable" : "", onclick: opts.onRowClick ? () => opts.onRowClick(r) : null },
    ...columns.map((c) => {
      const v = c.render ? c.render(r, ri) : esc(r[c.key]);
      return h("td", {}, v instanceof Node ? v : v);
    }))));
  return h("div", { class: "table-wrap" }, h("table", { class: "table" }, thead, tbody));
}

/** 通用分页器（D128）：全站列表统一用它，别再每页手写上一页/下一页。
 *  opts: { total, page, pageSize, onChange(page, pageSize), sizes=[20,50,100], unit="条" }
 *  渲染：总数/页数 + 首/末/上/下 + 页码（窗口式，最多 5 个）+ 每页条数切换。 */
export function pager({ total = 0, page = 1, pageSize = 20, onChange, sizes = [20, 50, 100], unit = "条" } = {}) {
  const pages = Math.max(1, Math.ceil((total || 0) / pageSize));
  const cur = Math.min(Math.max(1, page), pages);
  const go = (p) => onChange && onChange(p, pageSize);
  const setSize = (s) => onChange && onChange(1, s);
  const btn = (label, target, disabled, title) =>
    h("button", { class: "btn ghost sm", disabled: disabled ? "disabled" : null, title: title || "",
      onclick: () => go(target) }, label);
  // 页码窗口：当前页居中，左右各 2 页（超长列表也不撑爆工具栏）
  const win = [];
  const from = Math.max(1, cur - 2), to = Math.min(pages, cur + 2);
  for (let i = from; i <= to; i++) win.push(i);
  return h("div", { class: "pager" },
    h("span", { class: "muted" }, `共 ${total} ${unit} · 第 ${cur}/${pages} 页`),
    btn("首页", 1, cur <= 1),
    btn("上一页", cur - 1, cur <= 1),
    ...win.map((i) => h("button", { class: `btn ghost sm${i === cur ? " active" : ""}`,
      onclick: () => go(i) }, String(i))),
    btn("下一页", cur + 1, cur >= pages),
    btn("末页", pages, cur >= pages),
    h("span", { class: "muted", style: "margin-left:4px" }, "每页"),
    h("select", { class: "vs-input sm", style: "width:82px",
      onchange: (e) => setSize(Number(e.target.value)) },
      ...sizes.map((s) => h("option", { value: String(s), selected: s === pageSize ? "" : null }, `${s}`))));
}

/** KPI 行：一排带卡片底色的计数块（服务监控 / 案例库等页面共用）。
 *
 *  为什么提到 ui.js：案例库页第一版自己拼了 `.kpi / .kpi-value / .kpi-label` 三个类名，
 *  而这三个类**在 CSS 里根本不存在**——渲染出来就是一坨没有背景的裸文字（用户实测截图点名）。
 *  统一到这里：样式只有一处、页面不可能再各写一套。
 */
export function kpiRow(items) {
  return h("div", { class: "kpi-row" }, ...(items || []).map((it) =>
    h("div", { class: `kpi-item ${it.cls || ""}` },
      // 取值可以是数字，也可以是**节点**：状态型取值（"已启用/已关闭"）应渲染成徽章——
      // 把"已启用"三个字按数字字号（24px）放大成绿色巨字，又大又丑（用户实测反馈）。
      it.value instanceof Node
        ? h("div", { class: "kpi-v kpi-v-text" }, it.value)
        : h("div", { class: "kpi-v" }, String(it.value)),
      h("div", { class: "kpi-l muted" }, it.label))));
}

export function statCard(label, value, sub, cls = "") {
  // D42：把数值尾部的单位（%/分钟/小时…）拆成小一号的 .unit，数字本身更有冲击力
  // D54-1：单位必须以**非数字**开头——旧正则 (.+) 在纯数字值上会把最后一个数字回溯成"单位"，
  // 155 被拆成 15+小5、20 被拆成 2+小0（用户实测截图）。
  const m = String(value).match(/^([\d.,]+)(\D.*)$/);
  const val = m ? [m[1], h("span", { class: "unit" }, m[2])] : String(value);
  return h("div", { class: `stat-card ${cls}` },
    h("div", { class: "stat-label" }, label),
    h("div", { class: "stat-value mono" }, ...val),
    sub ? h("div", { class: "stat-sub muted" }, sub) : null);
}

export function formRow(label, input) { return h("div", { class: "form-row" }, h("label", {}, label), input); }
export function input(attrs = {}) { return h("input", { class: "vs-input", ...attrs }); }
export function select(options, value, attrs = {}) {
  const s = h("select", { class: "vs-input", ...attrs });
  options.forEach(([v, t]) => s.append(h("option", { value: v, selected: String(v) === String(value) ? "selected" : null }, t)));
  return s;
}

// ── D59 表单必填体系：刚需字段标红星 + 提交前统一拦截，别等后端 400 才提示 ──
/** 必填标记（放在 frow 的 label 位）：红星 + 提示语义。 */
export const reqLabel = (text) => h("span", { class: "req-wrap", title: "必填" },
  h("span", { class: "req", "aria-hidden": "true" }, "*"), text);
/** 非空 + 长度校验。返回错误文案，"" = 通过。v 会先 trim（"   " 视为空）。 */
export const needText = (v, label, max = 0) => {
  const s = String(v ?? "").trim();
  if (!s) return `${label}为必填项`;
  if (max && [...s].length > max) return `${label}过长（≤${max} 个字符）`;
  return "";
};
/** URL 校验（必填 http/https 开头）。空值不算它的错——必填性交给 needText。 */
export const needUrl = (v, label, max = 255) => {
  const s = String(v ?? "").trim();
  if (!s) return "";
  if (!/^https?:\/\//i.test(s)) return `${label}需以 http:// 或 https:// 开头`;
  if ([...s].length > max) return `${label}过长（≤${max} 个字符）`;
  return "";
};

// ECharts 封装：随主题重绘（规范 §6）
export function chart(el) {
  // 同一 DOM 上已有实例时先释放：主题重绘/切页重渲染会重复调用，否则实例泄漏（D3 实测发现）
  const stale = echarts.getInstanceByDom(el);
  if (stale) stale.dispose();
  const inst = echarts.init(el, null, { renderer: "canvas" });
  const resize = () => inst.resize();
  window.addEventListener("resize", resize);
  return inst;
}
export function themeColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (n, fb = "") => (cs.getPropertyValue(n).trim() || fb);
  return {
    accent: v("--accent", "#2383e2"), pass: v("--pass"), fail: v("--fail"), warn: v("--warn"),
    skip: v("--skip"), text: v("--text"), muted: v("--muted"),
    border: v("--border"), borderSoft: v("--border-soft"), hairline: v("--hairline"),
    surface: v("--surface"), surface2: v("--surface2"),
    /* D50：透明度派生档统一从令牌读 —— JS 里不再出现 rgba(35,131,226,…) 之类的字面值，
       换色/换主题时图表自动跟随（原来硬编码的 tooltip 在深色主题下是一张白卡）。 */
    accentWeak: v("--accent-weak"), accentSoft: v("--accent-soft"),
    accentGlow: v("--accent-glow"), accentRing: v("--accent-ring"),
    passWeak: v("--pass-weak"), failWeak: v("--fail-weak"), warnWeak: v("--warn-weak"),
    overlay1: v("--overlay-1"), overlay2: v("--overlay-2"),
  };
}
/* 坐标轴基准样式（D42 精细化）。
   原实现 splitLine 透明 + 保留轴线 → 图表"飘"、缺参照。
   改为：隐去轴线/刻度线（更干净），只留**极淡虚线网格**提供读数参照，
   标签用等宽小字。所有页面共用，属全局视觉提升。 */
/** 图表 tooltip 的统一精细样式（D42 建立；D50 改为**读令牌** → 深色主题不再是"白卡"）。 */
export function tooltipStyle() {
  const c = themeColors();
  return {
    backgroundColor: c.surface2,
    borderColor: c.border,
    borderWidth: 1,
    padding: [8, 11],
    extraCssText: `border-radius:8px;box-shadow:0 4px 14px ${c.overlay2};`,
    textStyle: { color: c.text, fontSize: 11.5 },
    axisPointer: { type: "line", lineStyle: { color: c.accentRing, type: [3, 3] } },
  };
}

export function baseAxis() {
  const c = themeColors();
  const hair = c.hairline || c.border;
  return {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: c.muted, fontSize: 10.5, fontFamily: "var(--font-mono)", margin: 10 },
    splitLine: { lineStyle: { color: hair, width: 1, type: [4, 4] } },
  };
}
/* 绝对时间格式化（D37）。
   后端统一存 UTC（naive，无时区后缀）；本函数解析为 UTC 后按**浏览器本地时区**显示，
   这样客户在任何时区都看到自己的本地时间。此前直接截字符串 → 平台所有绝对时间
   都比本地早 8 小时（如审计、告警触发、心跳），且巡检 cron 的 UTC 语义也易被误解。 */
export const fmtTime = (s) => {
  if (!s) return "-";
  const d = new Date(String(s).replace(" ", "T") + (String(s).includes("Z") ? "" : "Z"));
  if (isNaN(d.getTime())) return String(s).replace("T", " ").slice(0, 19);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};
/** 字节 → 可读大小（磁盘 df -h 表用）。 */
export const fmtBytes = (n) => {
  if (n == null || isNaN(n) || n < 0) return "-";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${i === 0 || v >= 100 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
};
/** 伪文件系统 fstype 黑名单——磁盘视图中只列真实关键分区，不刷 tmpfs/overlay 噪音。 */
export const REAL_FS_SKIP =
  /^(tmpfs|devtmpfs|overlay|proc|sysfs|devpts|cgroup\d?|squashfs|ramfs|debugfs|tracefs|securityfs|pstore|mqueue|autofs|configfs|fusectl|binfmt_misc|efivarfs|hugetlbfs)$/;
export const realDisks = (parts) => (parts || []).filter((p) => {
  const fs = String(p.fstype || "").toLowerCase();
  if (REAL_FS_SKIP.test(fs)) return false;
  const m = String(p.mount || "");
  if (["/run", "/dev/shm", "/sys", "/proc", "/dev"].includes(m)) return false;  // fstype 未命中时的伪路径兜底
  return true;
});
export const relTime = (s) => {
  if (!s) return "-";
  const d = new Date(String(s).replace(" ", "T") + (String(s).includes("Z") ? "" : "Z"));
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (isNaN(sec)) return fmtTime(s);
  if (sec < 0) return fmtTime(s);   // 未来时间戳（时区异常）→ 直接显示绝对时间，不显示负数
  if (sec < 60) return `${sec}秒前`;
  if (sec < 3600) return `${Math.floor(sec / 60)}分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}小时前`;
  return `${Math.floor(sec / 86400)}天前`;
};

/* ─────────────── D44 专业图标体系（替代 emoji）───────────────
   用户要求：不用表情符号（显得不专业），改专业、通用、好理解、无 AI 味的线性图标。
   统一用 **Feather 线性图标**（与侧栏导航同一套：24×24 viewBox、stroke、圆角端点），
   通过 currentColor 继承文字色，随主题/状态自动变色。 */
const ICON_PATHS = {
  // 警示/异常（替代 ⚠️）
  alert: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  // 正常/通过（替代 ✅）
  check: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
  // 失败/不可用（替代 ❌）
  x: '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
  // 锁定（替代 🔒）
  lock: '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  // 试用/计时（替代 ⏳）
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  // 思考中（替代 💭）
  thinking: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  // 主题（替代 ◐）
  theme: '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor" stroke="none"/>',
  // 信息
  info: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
  // 空态
  inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  // 活动/实时
  activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  // 刷新（替代 ↻；D49 AI 助手「立即更新推荐问题」用）
  refresh: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
};

/** 生成一个线性图标元素（继承 currentColor，随主题/状态变色）。 */
export function svgIcon(name, size = 14) {
  const path = ICON_PATHS[name] || ICON_PATHS.info;
  const span = document.createElement("span");
  span.className = `ico ico-${name}`;
  span.setAttribute("aria-hidden", "true");
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" `
    + `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;
  return span;
}

/** 图标 + 文字的内联组合（用于状态文字，替代 emoji 前缀）。 */
export function iconText(name, text, cls = "", size = 13) {
  return h("span", { class: `ico-text ${cls}` }, svgIcon(name, size), h("span", {}, text));
}
