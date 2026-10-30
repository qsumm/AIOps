// whatsnew.js —— 版本感知的更新公告（FR-421）
//
// 要解决的真问题：**功能迭代真做完了，但用户不感知**。更新只落在《用户手册》的
// 「修订记录」表里——要用户主动翻手册、翻到表、还看得懂「新增功能 / 存量更新」两列。
// 等于没有通知。本模块把"这次你多了什么"直接推到用户眼前。
//
// 三级打扰模型（关键设计，**别简化成"每次都弹"**）：
//   major —— 功能级更新  → 登录后自动弹**分栏大卡**（对齐产品发布公告的观感）
//   minor —— 优化/修订   → 右上角**轻量卡片**，不遮挡主界面
//   patch —— 内部修复    → 完全不打扰（只在「更新中心」里可查）
//   一次发版只弹最上面那一条；其余进入未读计数，不排队骚扰。
//
// 已读状态：localStorage 记**已读条目的 id**（不是版本号）——版本号会变、条目 id 稳定。
// 关闭弹窗即视为已读；全量历史常驻「更新中心」，不因关闭而丢失。
//
// 视觉纪律（AGENTS.md + 前端风格规范 + D50 调色板纪律）：
//   只用平台既有令牌（--accent / --surface / --border / --text / --muted）；
//   **禁暖色**（色相 10°~75°）、禁新配色、禁 CSS 框架、禁 CDN 资源；
//   右侧视觉区用纯 CSS/SVG 绘制（品牌脉搏标记 + 心电波形），不引外部图片。

import { h } from "./ui.js";
import { t, lang } from "./i18n.js";
import { RELEASES, localized } from "./changelog.js";
import { mountPixelScene } from "./wn_pixelscene.js";

const SEEN_KEY = "ops_seen_release";
const POPPED_KEY = "ops_popped_release";

/* ── 已读状态 ───────────────────────────────────────────── */

// 已读的最新条目 id（空 = 从未读过）
export function seenId() {
  try { return localStorage.getItem(SEEN_KEY) || ""; } catch { return ""; }
}

export function markSeen(id) {
  try { if (id) localStorage.setItem(SEEN_KEY, id); } catch { /* 隐私模式下忽略 */ }
  paintBadge();   // 已读数变了，顶栏角标跟着走
}

// 未读条目（最新在前）：比"已读"更新的那些
export function unreadEntries() {
  const seen = seenId();
  if (!seen) return RELEASES.slice();
  const i = RELEASES.findIndex((r) => r.id === seen);
  return i < 0 ? RELEASES.slice() : RELEASES.slice(0, i);
}

export function unreadCount() {
  return unreadEntries().length;
}

/* ── 公共零件 ───────────────────────────────────────────── */

// 右侧视觉区（纯 CSS/SVG 绘制，**不引任何位图**）
//
// 这一栏被否过三次，教训都留着，别再走回头路：
//   D130 玻璃拟态 + 高斯发光 + 细网格 + 版本水印 + 假 KPI（CPU 82% / MEM 74%）→ 用户判「太AI了」；
//   D132 改用图像模型生成「套印海报」→ 用户判「丑死了」，**整体回退**（生成物一并删除）。
// 结论（也是本版的立足点）：视觉资产要么靠**产品自己的视觉语言**手绘（可控、可复现、无二进制资产），
// 要么就不放。半吊子的"生成插画"只是换一种方式碰运气。
//
// 做法：一块**纯色**纸面（不渐变/不模糊/不发光/不叠网格）+ 一条**三通道指标带**——
// 用平台**真实指标名**（cpu.percent / mem.percent / disk.percent，与 AGENT_METRIC_CONTRACT 同源），
// 轨迹的形态取自平台自己的判读文档（CPU 周期性冲高 / 内存阶梯上升=泄漏 / 磁盘单调上升=日志堆积），
// 一条竖线贯穿三通道标出"故障从哪一刻开始"（正是本次发布的主打能力：图表叠加告警标记线）。
//
// 右栏视觉区（D133）：像素插画「运维工程师在双屏前值守」——由 wn_pixelscene.js **代码手绘**。
//
// 为什么不再用旧的两套：
//   D130 玻璃拟态 + 高斯发光 + 假 KPI  → 用户判「太AI了」
//   D131 纯 CSS/SVG 信号带（读数带）    → 干净但缺叙事，像图表不像人
//   D132 图像模型生成套印海报          → 用户判「丑死了」，整体回退
//   D133 逐格手绘像素画：可控、可复现、无二进制资产、可动画，且不冒充任何读数。
// 纪律：纯色块、无渐变/发光/玻璃/假数据（由 test_d130_whatsnew.py 机械守卫）。

function currentVersion() {
  return (typeof window !== "undefined" && window.__appVersion) || "";
}

// 变更条目列表（弹窗与更新中心共用）
function itemList(entry, cls) {
  const ul = h("ul", { class: cls });
  (entry.items || []).forEach((it) => {
    ul.append(
      h("li", {},
        h("b", { class: "wn-item-lead" }, localized(it, "lead")),
        h("span", { class: "wn-item-text" }, localized(it, "text"))),
    );
  });
  return ul;
}

/* ── ① 分栏大卡（major） ────────────────────────────────── */

let _lastFocus = null;

export function showMajorModal(entry) {
  if (!entry) return null;
  closeModal();

  _lastFocus = document.activeElement;
  const ver = currentVersion();
  const nItems = (entry.items || []).length;
  // 右栏关键词：取本次更新前 3 条的小标题——**真实发版内容**，不另编一套说法
  const tags = (entry.items || [])
    .slice(0, 3)
    .map((it) => localized(it, "lead"))
    .filter(Boolean)
    .join(" · ");

  const scene = mountPixelScene();   // 右栏像素画（关闭时必须 destroy 停动画）
  const dismiss = () => { markSeen(entry.id); closeModal(); };

  const mask = h("div", { class: "wn2-mask" });
  const cta = h("button", { class: "btn", type: "button" },
    t("whatsnew.start", "开始体验"));
  const later = h("button", { class: "btn secondary", type: "button" },
    t("whatsnew.full_log", "看完整更新日志"));

  // 「更新要点」：优先用 entry.highlights（一屏读完的短句 + 右侧数据锚点）；
  // 没有 highlights 的条目退回 items 的 lead/text，保证任何条目都不会弹空列表。
  const src = (entry.highlights && entry.highlights.length)
    ? entry.highlights
    : (entry.items || []).map((it) => ({ title: it.lead, desc: it.text }));
  const rows = src.map((hl) => h("div", { class: "wn2-item" },
    h("div", { class: "wn2-item-main" },
      h("div", { class: "wn2-item-title" }, localized(hl, "title")),
      h("div", { class: "wn2-item-desc" }, localized(hl, "desc"))),
    hl.stat ? h("div", { class: "wn2-item-stat" },
      h("div", { class: "wn2-stat-n mono" }, localized(hl, "stat")),
      h("div", { class: "wn2-stat-l" }, localized(hl, "statLabel"))) : null));

  const modal = h("div", {
    class: "wn2-modal",
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": "wn2-title",
  },
    h("div", { class: "wn2-main" },
      h("div", { class: "wn2-head" },
        h("span", { class: "wn2-pill mono" }, ver ? "v" + ver : "—"),
        h("span", { class: "wn2-when" }, entry.date || "")),
      h("h2", { class: "wn2-title", id: "wn2-title" }, localized(entry, "title")),
      h("p", { class: "wn2-sub" }, localized(entry, "summary")),
      h("div", { class: "wn2-list-label" }, t("whatsnew.highlights", "更新要点")),
      h("div", { class: "wn2-list" }, ...rows),
      h("div", { class: "wn2-foot" }, cta, later)),
    h("div", { class: "wn2-art" },
      // 顶栏：与底部图注同高同风格 → 上下两条窄栏把插画夹成完整"画框"，× 收进顶栏右端
      // （之前 × 直接压在插画右上角的亮窗上，既遮画面又像"贴膏药"）
      h("div", { class: "wn2-artbar" },
        h("span", {}, t("whatsnew.release_note", "发布说明")),
        h("button", { class: "wn2-close", type: "button", "aria-label": t("whatsnew.close", "关闭"),
          onclick: () => dismiss() }, closeIcon())),
      scene.el,
      h("div", { class: "wn2-caption mono" },
        h("span", {}, t("whatsnew.scene_kicker", "OPS · 值班中")),
        h("span", {}, ver ? "v" + ver : "—"))),
  );

  // 行为
  cta.addEventListener("click", () => {
    markSeen(entry.id);
    closeModal();
    if (entry.link) location.hash = entry.link;
  });
  // 「看完整更新日志」= 关掉弹窗并直接打开更新中心（历史常驻可查，信息不丢）
  later.addEventListener("click", () => { markSeen(entry.id); closeModal(); openCenter(); });
  mask.addEventListener("click", dismiss);

  const onKey = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); dismiss(); return; }
    if (e.key === "Tab") trapFocus(e, modal);
  };
  document.addEventListener("keydown", onKey, true);

  document.body.append(mask, modal);
  requestAnimationFrame(() => { mask.classList.add("open"); modal.classList.add("open"); });
  // 焦点入弹窗（键盘用户不会迷失）
  setTimeout(() => { try { cta.focus(); } catch { /* noop */ } }, 30);

  modal.__teardown = () => {
    document.removeEventListener("keydown", onKey, true);
    scene.destroy();          // 停掉像素画动画（不关会一直跑）
  };
  return modal;
}

function closeIcon() {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("width", "16");
  s.setAttribute("height", "16");
  s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "2");
  s.setAttribute("stroke-linecap", "round");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>';
  return s;
}

export function closeModal() {
  document.querySelectorAll(".wn-modal, .wn-mask, .wn2-modal, .wn2-mask").forEach((e) => {
    if (e.__teardown) e.__teardown();
    e.remove();
  });
  if (_lastFocus && _lastFocus.focus) { try { _lastFocus.focus(); } catch { /* noop */ } }
  _lastFocus = null;
}

// 焦点圈定在弹窗内（Tab 不跑出去）
function trapFocus(e, root) {
  const f = root.querySelectorAll(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
  if (!f.length) return;
  const first = f[0];
  const last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

/* ── ② 轻量卡片（minor） ────────────────────────────────── */

let _miniTimer = null;

export function showMinorCard(entry) {
  if (!entry) return null;
  clearMinor();

  const card = h("div", { class: "wn-mini", role: "status", "aria-live": "polite" },
    h("div", { class: "wn-mini-head" },
      h("span", { class: "wn-badge sm" }, localized(entry, "label")),
      h("button", { class: "wn-mini-x", type: "button", "aria-label": t("whatsnew.close", "关闭") },
        closeIcon())),
    h("div", { class: "wn-mini-title" }, localized(entry, "title")),
    itemList(entry, "wn-mini-list"),
    h("div", { class: "wn-mini-foot" },
      h("button", { class: "wn-mini-more", type: "button" },
        t("whatsnew.all_updates", "查看全部更新"))),
  );

  const dismiss = () => { markSeen(entry.id); clearMinor(); };
  card.querySelector(".wn-mini-x").addEventListener("click", dismiss);
  card.querySelector(".wn-mini-more").addEventListener("click", () => {
    markSeen(entry.id); clearMinor(); openCenter();
  });

  document.body.append(card);
  requestAnimationFrame(() => card.classList.add("open"));
  // 12s 后自动收起（不打断，也不长期占位）
  _miniTimer = setTimeout(() => { markSeen(entry.id); clearMinor(); }, 12000);
  return card;
}

export function clearMinor() {
  if (_miniTimer) { clearTimeout(_miniTimer); _miniTimer = null; }
  document.querySelectorAll(".wn-mini").forEach((e) => e.remove());
}

/* ── ③ 更新中心（历史常驻可查） ─────────────────────────── */

export function openCenter() {
  closeCenter();
  const seen = seenId();
  const ver = currentVersion();

  const mask = h("div", { class: "wn-center-mask" });
  const panel = h("aside", { class: "wn-center", role: "dialog", "aria-modal": "true", "aria-label": t("whatsnew.center", "更新中心") },
    h("div", { class: "wn-center-head" },
      h("h3", {}, t("whatsnew.center", "更新中心")),
      h("button", { class: "btn ghost", type: "button", onclick: () => closeCenter() },
        t("action.close", "关闭"))),
    h("div", { class: "wn-center-body" },
      h("div", { class: "wn-center-lead" },
        h("span", { class: "muted" }, t("whatsnew.current_version", "当前版本 · CURRENT")),
        h("span", { class: "mono" }, ver ? "v" + ver : "—")),
      ...RELEASES.map((r) => {
        const isNew = !seen || RELEASES.findIndex((x) => x.id === seen) > RELEASES.indexOf(r);
        return h("div", { class: "wn-rel" },
          h("div", { class: "wn-rel-head" },
            h("span", { class: "wn-rel-date mono" }, r.date || ""),
            r.version ? h("span", { class: "wn-rel-ver mono" }, "v" + r.version) : null,
            h("span", { class: "wn-badge sm" }, localized(r, "label")),
            isNew ? h("span", { class: "wn-rel-new" }, t("whatsnew.new_badge", "新")) : null),
          h("div", { class: "wn-rel-title" }, localized(r, "title")),
          itemList(r, "wn-rel-items"),
          r.link
            ? h("button", {
              class: "wn-rel-link", type: "button",
              onclick: () => { closeCenter(); location.hash = r.link; },
            }, t("whatsnew.view_detail", "查看详情"))
            : null);
      })),
  );

  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); closeCenter(); } };
  document.addEventListener("keydown", onKey, true);

  document.body.append(mask, panel);
  requestAnimationFrame(() => { mask.classList.add("open"); panel.classList.add("open"); });
  panel.__teardown = () => document.removeEventListener("keydown", onKey, true);
  return panel;
}

export function closeCenter() {
  // 遮罩类名与弹窗分开（wn-mask / wn-center-mask）——否则关一个会把另一个的遮罩也带走
  document.querySelectorAll(".wn-center, .wn-center-mask").forEach((e) => {
    if (e.__teardown) e.__teardown();
    e.remove();
  });
}

/* ── 触发 ───────────────────────────────────────────────── */

/**
 * 平台启动后调用一次。
 * 规则：只对**最上面那条**未读更新动手（不排队骚扰）；major 弹大卡、minor 出轻量卡、patch 不打扰。
 * 同一个条目每个会话只自动弹一次（localStorage 记 popped）。
 */
export function initWhatsNew() {
  const pending = unreadEntries();
  if (!pending.length) { paintBadge(); return; }
  const top = pending[0];

  let popped = "";
  try { popped = localStorage.getItem(POPPED_KEY) || ""; } catch { /* noop */ }
  if (popped === top.id) { paintBadge(); return; }   // 本会话已弹过

  if (top.level === "major") {
    showMajorModal(top);
    try { localStorage.setItem(POPPED_KEY, top.id); } catch { /* noop */ }
  } else if (top.level === "minor") {
    showMinorCard(top);
    try { localStorage.setItem(POPPED_KEY, top.id); } catch { /* noop */ }
  }
  // patch：完全不打扰（只在更新中心可查）
  paintBadge();
}

/** 顶栏铃铛的未读角标。 */
export function paintBadge() {
  const b = document.getElementById("wn-badge");
  if (!b) return;
  const n = unreadCount();
  b.hidden = n === 0;
  b.textContent = n > 99 ? "99+" : String(n);
}

/** 语言切换后需要重画（文案变了）。 */
export function refreshWhatsNew() {
  paintBadge();
}

export { RELEASES };
