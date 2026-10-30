// pages/manual.js —— 内置文档中心（D66 建立；D67 扩为多文档 + 布局修整）
//
// 内容真身是 docs/04-交付与运营/ 下的**客户版**文档，构建时随镜像落到 /static/manual/：
//   · 用户手册.md    → 分 15 节，是主体
//   · 快速开始.md    → 源码买家首次部署（5 节）
// 本页只做「取 + 渲染 + 导航」，不维护第二份内容（单一来源）。
//
// 关键实现点：
//   · 目录由正文 `## ` 标题自动解析（文档加节，目录自动跟），**跳过围栏代码块内的 ##**
//     （手册里的 SKILL.md 模板含 `## 何时用`，不排除会多出假章节——实测踩到）；
//   · 正文里的 `.md` 相对链接会被拦截并映射到对应文档——否则会打到后端 404，
//     界面显示 FastAPI 的 {"detail":"Not Found"}（用户实测报障）；
//   · 全文搜索跨全部文档（命中计数 + 自动跳到首个命中节 + 文本节点高亮）；
//   · 深链 #/manual/6（手册第 6 节）、#/manual/quickstart/2（某文档第 2 节）。
import { api } from "../api.js";
import { marked } from "/vendor/marked.esm.js";
import { h, toast } from "../ui.js";
import { lang } from "../i18n.js";   // D100：演示视频按语言选片

const DOCS = [
  { key: "manual", title: "使用手册", file: "manual.md" },
  { key: "quickstart", title: "快速开始", file: "quickstart.md" },
];
const IMG_BASE = "/manual/images/";
// 正文里的相对链接 → 内置文档 key（拦截后转为站内切换，避免 404）
const FILE_TO_DOC = { "用户手册.md": "manual", "快速开始.md": "quickstart" };   // 演示脚本为内部文档，不再内置展示（D101）

/** 目录/面包屑里显示的标题：**剥掉行内 markdown 标记**。
 *  为什么需要（用户实测）：手册里有个章节标题写成 `## 界面上的 **?** 怎么看`，
 *  正文渲染时 `**` 变粗体没问题，但目录与面包屑取的是**原始标题文本** →
 *  界面上直接显示 `界面上的 **?** 怎么看`，看着像打错了。这里统一剥掉，
 *  即使以后标题里带标记也不会再露出来（同时加了守卫禁止标题带标记）。
 */
const plainTitle = (t) => String(t || "")
  .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")   // [文字](链接) → 文字
  .replace(/[*`]/g, "")
  .trim();

let cache = null;   // [{key, title, chapters:[{n, title, body, html}]}]

function renderMd(md) {
  let html = "";
  try { html = marked.parse(md || "", { breaks: false, gfm: true }); } catch { html = `<p>${md}</p>`; }
  // 给 h3/h4 注入锚点 id（右侧「本页目录」的跳转目标；切章重建，id 只需章内唯一）
  let n = 0;
  html = html.replace(/<h([34])>/g, (mm, lv) => `<h${lv} id="sec-${++n}">`);
  // 演示视频按语言选片（D100）：EN → demo-en.mp4（英文配音+英文字幕）；ZH → demo.mp4
  const demoVid = lang() === "en" ? "demo-en.mp4" : "demo.mp4";
  return html
    .replace(/src="images\//g, `src="${IMG_BASE}`)
    .replace(/src="videos\//g, `src="/manual/videos/`)
    .replace(/poster="videos\//g, `poster="/manual/videos/`)
    .replace(/\/manual\/videos\/demo\.mp4/g, `/manual/videos/${demoVid}`);   // 子串替换：兼容 ?v=指纹 后缀
}

/** 解析一篇文档：按 `## ` 切节，跳过围栏代码块内的标题与文档自带的「目录」段。 */
function parseDoc(md) {
  const lines = md.split("\n");
  const chapters = [];
  let cur = null;
  let fence = false;
  for (const line of lines) {
    if (/^\s*(```|~~~)/.test(line)) { fence = !fence; if (cur) cur.body.push(line); continue; }
    if (fence) { if (cur) cur.body.push(line); continue; }
    const m = /^##\s+(.+?)\s*$/.exec(line);
    if (m) {
      const title = m[1];
      if (/^目录$/.test(title)) { cur = null; continue; }
      const num = /^(\d+)[.、]\s*(.*)$/.exec(title);
      // D101：无编号的前置/后置章（平台演示视频 / 界面上的问号 / 修订记录）→ n=null，
      // 不与正文章节的显式编号（"## 1. 登录…"）抢号——此前 TOC 会出现两个 "1"。
      cur = { n: num ? Number(num[1]) : null, title: plainTitle(num ? num[2] : title), body: [] };
      chapters.push(cur);
      continue;
    }
    if (/^#\s+/.test(line)) { cur = null; continue; }   // 文档大标题不进节
    if (cur) cur.body.push(line);
  }
  for (const c of chapters) { c.body = c.body.join("\n").trim(); c.html = renderMd(c.body); }
  return chapters;
}


/** D101：FAQ（常见问题）表格打标 —— Q/A 两列分宽、序号徽章化（样式见 .qa-table）。 */
function tagQaTable(body) {
  body.querySelectorAll("table").forEach((t) => {
    const ths = [...t.querySelectorAll("thead th")].map((x) => x.textContent.trim());
    if (ths[0] === "#" && ths[1] && ths[1].includes("现象")) t.classList.add("qa-table");
  });
}

/** D101 补：FAQ（常见问题）**段落形式**打标 —— 《快速开始 §四》用的是 `**Q：…**` + `A：…`
 * 段落结构（与《用户手册》表格版不同的另一种 FAQ 形态）。markdown 软换行不会换行，
 * Q 与 A 会被渲染到同一段里挤成一行。这里把 Q 行渲染成独立徽章行、A 渲染成独立答案块。
 * 背景：当初 D101 只覆盖了表格版（.qa-table），段落版一直漏网。 */
function tagQaParagraph(body) {
  body.querySelectorAll("p").forEach((p) => {
    const strong = p.querySelector("strong");
    const text = (p.textContent || "");
    if (!/^\s*Q[：:]/.test((strong && strong.textContent) || text)) return;
    // 情形①：Q 与 A 被 markdown 软换行挤在同一段 → 拆成两块
    if (/\bA[：:]/.test(text)) {
      p.classList.add("qa-par-q");
      const full = p.innerHTML;
      const aIdx = Math.max(full.indexOf("A："), full.indexOf("A:"));
      if (aIdx < 0) return;
      const strongHtml = full.slice(0, aIdx);
      const ansHtml = full.slice(aIdx).replace(/^\s*A[：:]\s*/, "");
      p.innerHTML = strongHtml;
      const ans = document.createElement("p");
      ans.className = "qa-par-a";
      ans.innerHTML = ansHtml;
      p.after(ans);
      return;
    }
    // 情形②：内容层已用空行分段（Q、A 已是相邻两段）→ 各自打标
    p.classList.add("qa-par-q");
    const a = p.nextElementSibling;
    if (a && a.tagName === "P" && /^\s*A[：:]/.test((a.textContent || "").trim()))
      a.classList.add("qa-par-a");
  });
}

/** 在已渲染 DOM 里高亮关键词（走文本节点，不碰标签与属性）。 */
function highlight(el, term) {
  if (!term) return;
  const t = term.toLowerCase();
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const targets = [];
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (n.nodeValue && n.nodeValue.toLowerCase().includes(t)) targets.push(n);
  }
  for (const node of targets) {
    const text = node.nodeValue;
    const parts = [];
    let i = 0;
    for (;;) {
      const idx = text.toLowerCase().indexOf(t, i);
      if (idx < 0) { parts.push(document.createTextNode(text.slice(i))); break; }
      if (idx > i) parts.push(document.createTextNode(text.slice(i, idx)));
      const mark = document.createElement("mark");
      mark.textContent = text.slice(idx, idx + term.length);
      parts.push(mark);
      i = idx + term.length;
    }
    node.replaceWith(...parts);
  }
}

function hitsIn(text, term) {
  if (!term) return 0;
  const lt = term.toLowerCase(), hay = text.toLowerCase();
  let n = 0, i = 0;
  for (;;) { const k = hay.indexOf(lt, i); if (k < 0) break; n++; i = k + lt.length; }
  return n;
}

export async function render(root, arg) {
  root.replaceChildren(h("div", { class: "empty" }, "加载文档…"));

  if (!cache) {
    const loaded = [];
    for (const d of DOCS) {
      try {
        const r = await fetch(`/manual/${d.file}`, { cache: "no-cache" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        loaded.push({ ...d, chapters: parseDoc(await r.text()) });
      } catch { /* 单篇缺失不阻断其余文档 */ }
    }
    if (!loaded.length) {
      root.replaceChildren(h("div", { class: "card" },
        h("h3", {}, "完整操作手册随公众号发放"),
        h("div", { class: "note" }, "开源包不含完整手册——关注公众号「一鸣AI编程」，转发文章后获取。"),
        h("div", { class: "muted", style: "font-size:0.78rem" },
          "部署与常见问题见仓库 README「部署」章节；本页仅在手册随镜像分发的构建中可用。")));
      return;
    }
    cache = loaded;
  }
  const docs = cache;

  // 起始位置：深链 #/manual/6（手册第 6 节）或 #/manual/quickstart/2
  const segs = String(arg || "").split("/").filter(Boolean);
  let curDoc = docs[0].key, curIdx = 0;
  if (segs.length === 2 && docs.some((d) => d.key === segs[0])) {
    const d = docs.find((x) => x.key === segs[0]);
    const i = Number(segs[1]) - 1;
    if (i >= 0 && i < d.chapters.length) { curDoc = d.key; curIdx = i; }
  } else if (segs.length === 1) {
    const n = Number(segs[0]);
    const d = docs[0];
    const i = d.chapters.findIndex((c) => c.n === n);
    if (i >= 0) { curDoc = d.key; curIdx = i; }
  }

  let term = "";

  // ── 骨架 ──
  const tocEl = h("nav", { class: "m-toc", "aria-label": "文档目录" });
  const docEl = h("article", { class: "m-doc" });
  const railEl = h("aside", { class: "m-rail", "aria-label": "本页目录" });
  let spy = null;   // 滚动高亮的观察器（切章/重绘时重建）
  const qEl = h("input", { class: "vs-input m-search", "aria-label": "搜索文档内容",
    placeholder: "搜索文档（如：探活 / 抑制 / 部署）" });
  const hitEl = h("span", { class: "m-hits muted" });

  root.replaceChildren(
    h("div", { class: "m-head" },
      h("div", { class: "m-head-left" },
        h("span", { class: "m-sync" }, "随平台内置"),
        h("span", { class: "m-sync-note muted" }, "与当前版本同步")),
      h("div", { class: "m-tools" }, hitEl, qEl)),
    h("div", { class: "m-body" }, tocEl, docEl, railEl));

  const cur = () => {
    const d = docs.find((x) => x.key === curDoc) || docs[0];
    return { d, c: d.chapters[Math.min(curIdx, Math.max(0, d.chapters.length - 1))] };
  };

  function drawToc() {
    const t = term.trim();
    let total = 0;
    const blocks = docs.map((d) => {
      const rows = d.chapters.map((c, i) => {
        const n = hitsIn(c.title + "\n" + c.body, t);
        total += n;
        const isCur = d.key === curDoc && i === curIdx;
        const dim = t && !n;
        return h("button", {
          class: "m-chap" + (isCur ? " on" : "") + (dim ? " dim" : ""),
          onclick: () => { curDoc = d.key; curIdx = i; drawToc(); drawDoc(); },
        }, h("span", { class: "m-chap-n" + (c.n == null ? " none" : "") }, c.n == null ? "·" : String(c.n)),
          h("span", { class: "m-chap-t" }, plainTitle(c.title)),
          n ? h("span", { class: "m-chap-hit" }, String(n)) : null);
      });
      return h("div", { class: "m-group" },
        h("div", { class: "m-group-title" }, d.title),
        ...rows);
    });
    tocEl.replaceChildren(...blocks);
    hitEl.textContent = t ? (total ? `命中 ${total} 处` : "无匹配") : "";
  }

  /** 右栏（D95）：本页目录（h3/h4 树、点击平滑跳转、滚动高亮）+ 快捷卡。
      参考 OceanBase 文档中心——把宽屏右侧空白变成有用的导航，而不是一整块留白。 */
  function buildRail(body) {
    if (spy) { spy.disconnect(); spy = null; }
    const heads = [...body.querySelectorAll("h3,h4")].map((el) => ({
      el, id: el.id, txt: el.textContent.trim(), lv: el.tagName === "H3" ? "lv3" : "lv4",
    }));
    const links = heads.map((it) => h("a", {
      class: "m-rail-it " + it.lv,
      onclick: () => it.el.scrollIntoView({ behavior: "smooth", block: "start" }),
    }, it.txt));
    railEl.replaceChildren(
      h("div", { class: "m-rail-t" }, "本页目录"),
      ...(heads.length ? links : [h("div", { class: "m-rail-empty muted" }, "本节无小节标题")]),
      h("div", { class: "m-rail-sep" }),
      h("div", { class: "m-rail-qa" },
        h("button", { class: "btn ghost m-rail-btn", onclick: () => { location.hash = "#/about"; } }, "版本信息")));
    if (!heads.length) return;
    // 滚动高亮：视口上部出现的第一个小节即为"当前"
    heads.forEach((it) => { it._vis = false; });
    spy = new IntersectionObserver((es) => {
      es.forEach((e) => { const it = heads.find((x) => x.el === e.target); if (it) it._vis = e.isIntersecting; });
      const act = heads.find((x) => x._vis);
      if (act) links.forEach((l, i) => l.classList.toggle("on", heads[i] === act));
    }, { root: docEl, rootMargin: "-12px 0px -55% 0px" });   // root=滚动容器（D102）
    heads.forEach((it) => spy.observe(it.el));
  }

  function drawDoc() {
    const { d, c } = cur();
    if (!c) { docEl.replaceChildren(h("div", { class: "empty" }, "该文档暂无可显示内容")); railEl.replaceChildren(); return; }
    const body = h("div", { class: "m-doc-body", html: c.html });
    const i = d.chapters.indexOf(c);
    const prev = d.chapters[i - 1], next = d.chapters[i + 1];
    const jump = (target) => () => { curIdx = d.chapters.indexOf(target); drawToc(); drawDoc(); };
    docEl.replaceChildren(
      h("div", { class: "m-doc-head" },
        // 面包屑用「文档 · 节标题 · 位置」——不用「第 N 节」：源文档编号可能从 0 起
        // （演示脚本就是「0. 开场」），照抄会出现"第 0 节"的怪读法。
        h("div", { class: "m-crumb muted" },
          h("span", { class: "m-crumb-doc" }, plainTitle(d.title)),
          " · ",
          h("span", { class: "m-crumb-sec" }, plainTitle(c.title)),
          h("span", { class: "m-crumb-pos" }, `　${i + 1} / ${d.chapters.length}`)),
        h("div", { class: "m-nav" },
          prev ? h("button", { class: "btn ghost m-step", onclick: jump(prev), title: prev.n == null ? prev.title : `${prev.n}. ${prev.title}` }, "← 上一节") : null,
          next ? h("button", { class: "btn ghost m-step", onclick: jump(next), title: next.n == null ? next.title : `${next.n}. ${next.title}` }, "下一节 →") : null)),
      body);
    buildRail(body);
    tagQaTable(body);
    tagQaParagraph(body);
    highlight(body, term.trim());
    bindDocLinks(body);
    docEl.scrollTo({ top: 0 });   // D102：滚动容器是 .m-doc 自己（外层框架不动）
  }

  /** 拦截正文里的 .md 相对链接：映射到内置文档（否则打到后端 → {"detail":"Not Found"}）。 */
  function bindDocLinks(container) {
    container.querySelectorAll("a[href]").forEach((a) => {
      const href = a.getAttribute("href") || "";
      if (/^https?:/i.test(href)) { a.target = "_blank"; a.rel = "noreferrer"; return; }
      if (!/\.md(#.*)?$/i.test(href)) return;
      const fname = decodeURIComponent(href.split("#")[0].split("/").pop());
      const key = FILE_TO_DOC[fname];
      a.addEventListener("click", (e) => {
        e.preventDefault();
        const target = key && docs.find((x) => x.key === key);
        if (target) { curDoc = target.key; curIdx = 0; drawToc(); drawDoc(); }
        else toast(`《${fname.replace(/\.md$/, "")}》未随平台内置，请查看交付包内的文档目录`, "warn");
      });
      if (!key) a.classList.add("m-link-ext");
      a.title = key ? `打开《${fname.replace(/\.md$/, "")}》` : "该文档未随平台内置";
    });
  }

  let timer = null;
  function applySearch() {
    term = qEl.value;
    if (term.trim()) {
      const { c } = cur();
      if (!hitsIn(c.title + "\n" + c.body, term)) {
        outer: for (const doc of docs) {
          for (let i = 0; i < doc.chapters.length; i++) {
            if (hitsIn(doc.chapters[i].title + "\n" + doc.chapters[i].body, term)) {
              curDoc = doc.key; curIdx = i; break outer;
            }
          }
        }
      }
    }
    drawToc();
    drawDoc();
  }
  qEl.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(applySearch, 140); });
  qEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); clearTimeout(timer); applySearch(); }
    if (e.key === "Escape") { qEl.value = ""; clearTimeout(timer); applySearch(); }
  });

  drawToc();
  drawDoc();
}

export function destroy() {}
