// pages/login.js —— 登录页（D24：按平台现行 editorial/C 版登录风格实现，
// 含流星层（8 匀速 + 1 随机加速）与 Manifesto 换一句；只换文案，不换设计。
// 规范 §7 已同步：原 E 方案已被 平台前端规范上游取代，本文档为其一比一复刻记录。
// D97：用户可见文案改用 t() —— 切到英文后全英文（其余页面内联中文不在本轮范围）。
import { api, login, getToken } from "../api.js";
import { h } from "../ui.js";
import { t, lang } from "../i18n.js";

/* 工程派短句池（中英双填，D97：切到英文时 login.js 整页 0 中文字符——manifesto 也得能切）。
   h=主句 b=副句，无中文句号 */
const QUOTES = [
  { zh: { h: "运维不该是盯屏,而是工程的一部分", b: "把每一条告警变成可追溯的处置, 把每一次故障变成下一次的改进" },
    en: { h: "Ops shouldn't be staring at screens — it should be engineering.",
         b: "Every alert becomes a traceable action; every incident, a future improvement." } },
  { zh: { h: "每一台主机都在被持续体检, 没有任何一台被跳过", b: "把巡检变成运维流程的默认路径, 而不是出事后的临时补救" },
    en: { h: "Every host is under continuous health-check — none skipped.",
         b: "Make inspections the default path, not the after-the-fact patch." } },
  { zh: { h: "告警不是终点, 它是改进的起点", b: "让每一次触发都留下指纹与证据链, 让稳定在工程里自动生长" },
    en: { h: "An alert is not the end — it's the start of improvement.",
         b: "Every trigger leaves a fingerprint and evidence chain; stability grows in the engineering itself." } },
  { zh: { h: "把故障诊断变成产品迭代的呼吸", b: "每一次告警收敛, 都在让系统更接近它应该成为的样子" },
    en: { h: "Let incident diagnosis become the breath of product iteration.",
         b: "Each convergence pulls the system closer to what it should be." } },
  { zh: { h: "稳定不是靠人守出来的, 是被工程构建出来的", b: "把每一条规则嵌入采集流水线, 让巡检变成免费的副产物" },
    en: { h: "Stability isn't earned by human vigilance — it's built by engineering.",
         b: "Embed every rule in the collection pipeline; inspections become free byproducts." } },
  { zh: { h: "让 AI 与运维站在同一条流水线里", b: "把每一次根因报告当作工程改进的契机, 而不只是群里的红字" },
    en: { h: "AI and ops on the same line.",
         b: "Every RCA report is an engineering improvement, not a red word in a chat room." } },
  { zh: { h: "好的巡检不是写在文档里的, 是被调度在 CRON 里的", b: "把每一次体检变成流水线的呼吸, 让值班不需要人盯" },
    en: { h: "A good inspection isn't written in docs — it's scheduled in CRON.",
         b: "Each health-check is a breath of the pipeline; no one needs to watch." } },
  { zh: { h: "AI 盯盘, 工程跑处置, 用户感受到安稳", b: "把运维从人力密集的值守, 变成工程流水线里安静的一环" },
    en: { h: "AI watches, engineering handles, users feel stable.",
         b: "Ops goes from human-dense watch to a quiet part of the engineering pipeline." } },
];
/* 按当前 lang 选语录（中英两套，User-agent 切到 EN 后显示英文版） */
function pickQuotes() { return QUOTES.map((q) => q[lang() === "en" ? "en" : "zh"]); }

export function render(root) {
  document.body.classList.add("login-mode");

  /* ── 流星层：8 颗匀速（16s 周期、负延迟错峰）+ 1 颗随机加速（mt-rush）──
     参数与平台既有登录页逐值一致；纪律 = 长周期 + 只动 transform/opacity。 */
  const MT = [
    ["24%", "-20%", "150px", "16s", "0s", ".7"], ["54.6%", "4%", "110px", "16s", "-2s", ".86"],
    ["85.2%", "-12%", "140px", "16s", "-4s", ".6"], ["34.2%", "16%", "120px", "16s", "-6s", ".9"],
    ["64.8%", "-16%", "155px", "16s", "-8s", ".74"], ["95.4%", "8%", "100px", "16s", "-10s", ".56"],
    ["44.4%", "22%", "130px", "16s", "-12s", ".82"], ["75%", "-6%", "145px", "16s", "-14s", ".66"],
  ];
  const meteors = h("div", { class: "lqa-meteors", "aria-hidden": "true" },
    ...MT.map(([l, t, len, tt, dl, op]) =>
      h("span", { class: "mt", style: `left:${l};top:${t};--len:${len};--t:${tt};--dl:${dl};--op:${op}` }, h("i"))),
    h("span", { class: "mt mt-rush", style: "left:46%;top:-14%;--len:170px;--t:3.6s;--op:.8" }, h("i")));
  const rush = meteors.querySelector(".mt-rush");

  /* 那"一条随机的"：每飞完一圈重摇起点/长度/亮度/时长（平台既有实现同款脚本） */
  const rnd = (a, b) => a + Math.random() * (b - a);
  const roll = () => {
    rush.style.left = rnd(22, 96).toFixed(1) + "%";
    rush.style.top = rnd(-26, 30).toFixed(1) + "%";
    rush.style.setProperty("--len", Math.round(rnd(90, 235)) + "px");
    rush.style.setProperty("--op", rnd(0.5, 0.92).toFixed(2));
    rush.style.animationDuration = rnd(3, 4.4).toFixed(2) + "s";
  };
  roll();
  rush.addEventListener("animationiteration", roll);

  /* ── Manifesto 换一句 ── */
  const q1 = h("p", { id: "lq1" }, pickQuotes()[7].h);
  const q3 = h("p", { id: "lq3", class: "quote-sep" }, pickQuotes()[7].b);
  const qid = h("span", { id: "lqid" }, "#008");
  function reroll() {
    const i = Math.floor(Math.random() * QUOTES.length);
    q1.textContent = pickQuotes()[i].h;
    q3.textContent = pickQuotes()[i].b;
    qid.textContent = "#" + String(i + 1).padStart(3, "0");
  }
  reroll();
  const refreshHint = h("div", { class: "refresh-hint", id: "lrefresh", onclick: reroll }, t("login.refresh_quote"));

  /* ── 右侧表单（沿用既有登录/演示逻辑）── */
  const user = h("input", { type: "text", id: "login-username", placeholder: t("login.username_ph"), autocomplete: "username", value: "" });
  const pwd = h("input", { type: "password", id: "login-password", placeholder: t("login.password_ph"), autocomplete: "current-password", value: "" });
  const err = h("div", { id: "login-error", class: "login-error" });
  const btn = h("button", { type: "submit", id: "btn-login", class: "btn" }, t("login.btn"));
  [user, pwd].forEach((el) => el.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); }));

  async function submit() {
    err.textContent = "";
    btn.disabled = true; btn.textContent = t("common.saving") === "common.saving" ? "登录中…" : t("common.saving").replace("Saving…", "登录中…");
    try {
      await login(user.value.trim(), pwd.value);
      location.hash = "#/dashboard";
      location.reload();
    } catch (e) {
      err.textContent = e.message || t("err.load_failed");
    } finally { btn.disabled = false; btn.textContent = t("login.btn"); }
  }

  root.replaceChildren(h("div", { id: "login-screen", class: "login-screen" },
    meteors,
    h("div", { class: "split" },
      h("div", { class: "left" },
        h("div", { class: "brand" },
          h("span", { class: "logo-wrap", html: '<svg width="34" height="34" viewBox="96 96 320 320" aria-hidden="true">'
          + '<rect x="96" y="96" width="320" height="320" rx="76" fill="#1A1A1F"/>'
          + '<polyline points="132,258 196,258 228,192 270,302 302,238 318,238" fill="none" stroke="#fff" stroke-width="34" stroke-linecap="round" stroke-linejoin="round"/>'
          + '<circle cx="352" cy="238" r="21" fill="#2383E2"/></svg>' }),
          h("div", {},
            h("div", { class: "nm" }, t("brand")),
            h("div", { class: "sub" }, "TESTOPS MONITORING PLATFORM"))),
        h("div", { class: "quote-wrap" },
          h("div", { class: "quote" },
            q1,
            h("p", { class: "quote-sep" }, "—"),
            q3),
          h("div", { class: "credit" }, "— TESTOPS MANIFESTO · v1.0 · ", qid),
          refreshHint),
        h("div", { class: "stamp" }, t("brand"), h("span", { id: "login-ver" }, ""))),
      h("div", { class: "divider-v" }),
      h("div", { class: "right" },
        h("div", { class: "tag" }, t("login.welcome")),
        h("div", { class: "h1" }, t("login.welcome")),
        // 演示模式一键入口已下架（公网部署安全，2026-10-15）：demo 账号仍可经
        // /api/auth/demo 进入（受 DEMO_ENABLED 开关控制），但不再提供公开直达链接
        h("form", { novalidate: "novalidate", onsubmit: (e) => { e.preventDefault(); submit(); } },
          h("div", { class: "field" }, h("label", {}, t("login.label.username")), user),
          h("div", { class: "field" }, h("label", {}, t("login.label.password")), pwd),
          h("div", { class: "row" },
            h("label", {}, h("input", { type: "checkbox", checked: "checked" }), h("span", {}, t("login.keep"))),
            h("a", { href: "#", onclick: (e) => e.preventDefault() }, t("login.forgot"))),
          err,
          btn)))));

  // D66：登录页也显示真实版本号（客户截图反馈时最常缺的信息）。
  // 失败静默：版本取不到不影响登录。
  api.get("/api/health").then((d) => {
    const el = document.getElementById("login-ver");
    if (el && d.version) el.textContent = ` · v${d.version}`;
  }).catch(() => {});

  function destroyListener() { rush.removeEventListener("animationiteration", roll); }
  root.addEventListener("DOMContentLoaded", destroyListener, { once: true });
  return { destroyListener };
}

export function destroy() { document.body.classList.remove("login-mode"); }
export const isAuthed = () => !!getToken();
