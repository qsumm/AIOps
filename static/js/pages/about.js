// pages/about.js —— 版本信息（D66：与「使用手册」同属侧栏「文档」组）
//
// 为什么单独成页而不是弹层：版本号/构建号/License 是**排障时要索取的信息**，
// 需要一个可指向、可复制的稳定位置（反馈问题时让对方「打开 文档 → 版本信息，点复制」）。
// 数据源：/api/health（version/build/build_time，壳层已缓存到 window.__app*）+ /api/license/state。
import { api } from "../api.js";
import { h, toast, copyText, fmtTime } from "../ui.js";
import { t } from "../i18n.js";
import { openCenter } from "../whatsnew.js";
import { RELEASES, localized } from "../changelog.js";

const LIC_LABEL = {
  valid: ["有效", "PASS"],
  grace: ["宽限期", "WARN"],
  locked: ["未授权", "FAIL"],
  disabled: ["未启用强制", "SKIP"],
};

export async function render(root) {
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  let lic = null;
  try { lic = await api.get("/api/license/state"); } catch { /* 取不到不影响版本展示 */ }

  const version = window.__appVersion || "";
  const build = window.__appBuild || "";
  const buildTime = window.__appBuildTime || "";
  const [licText, licCls] = LIC_LABEL[(lic && lic.state) || "disabled"] || ["未知", "SKIP"];

  // 复制给技术方的一整段（排障第一句话就是问版本）
  const copyText_ = [
    "【AI 运维监控平台 · 版本信息】",
    `版本：${version ? "v" + version : "未知"}`,
    `构建：${build || "dev"}${buildTime ? " · " + buildTime : ""}`,
    `License：${licText}${lic && lic.expires_at ? " · 到期 " + String(lic.expires_at).slice(0, 10) : ""}`,
    `页面地址：${location.origin}`,
    `浏览器：${navigator.userAgent}`,
  ].join("\n");

  const kv = (k, v) => h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v));

  root.replaceChildren(
    h("div", { class: "about-page" },
      // 页题在顶栏（D44 纪律）；此处只留位置提示
      h("div", { class: "about-head" },
        h("span", { class: "muted", style: "font-size:0.78rem" }, "文档 → 版本信息")),

      h("div", { class: "card about-card" },
        h("div", { class: "about-ver" },
          h("span", { class: "about-ver-label muted" }, "当前版本"),
          h("span", { class: "about-ver-num mono" }, version ? `v${version}` : "未知")),
        h("div", { class: "detail-meta about-meta" },
          kv("产品", "AI 运维监控平台"),
          kv("构建", h("span", { class: "mono" }, build || "dev")),
          kv("构建时间", h("span", { class: "mono" }, buildTime ? fmtTime(buildTime) : "—")),
          kv("License", h("span", { class: `badge ${licCls}` }, licText)))),

      lic ? h("div", { class: "card" },
        h("h3", {}, "License 状态"),
        h("div", { class: "detail-meta" },
          kv("模块", h("span", { class: "mono" }, (lic.modules || []).join(", ") || "-")),
          kv("到期", h("span", { class: "mono" }, lic.expires_at ? String(lic.expires_at).slice(0, 10) : "—")),
          lic.grace_days_left != null ? kv("宽限剩余", h("span", { class: "mono" }, `${lic.grace_days_left} 天`)) : null),
        lic.reason ? h("div", { class: "note", style: "margin-top:10px" }, lic.reason) : null) : null,

      // FR-421：更新历史入口（弹窗被关掉也不丢信息——这里常驻可查）
      h("div", { class: "card" },
        h("h3", {}, t("whatsnew.center", "更新中心")),
        h("div", { class: "about-hist" },
          ...RELEASES.slice(0, 3).map((r) => h("div", { class: "about-hist-row" },
            h("div", { class: "about-hist-head" },
              h("span", { class: "mono muted", style: "font-size:0.74rem" }, r.date || ""),
              r.version ? h("span", { class: "mono muted", style: "font-size:0.74rem" }, "v" + r.version) : null,
              h("span", { class: "wn-badge sm" }, localized(r, "label"))),
            h("div", { class: "about-hist-title" }, localized(r, "title"))))),
        h("button", { class: "btn secondary", style: "margin-top:12px", onclick: openCenter },
          t("whatsnew.all_updates", "查看全部更新"))),

      h("div", { class: "about-actions" },
        h("button", { class: "btn", onclick: () => copyText(copyText_,
          () => toast("版本信息已复制，可直接粘贴给技术支持", "ok"),
          () => toast("浏览器限制未自动复制，请手动选中复制", "warn")) }, "复制版本信息"),
        h("a", { class: "btn secondary", href: "#/manual" }, "查看使用手册")),

      h("div", { class: "muted about-foot" },
        "版本号与《版本发布说明》一一对应；遇到问题时附上本页信息，可显著加快定位。" +
        "手册与版本信息随平台镜像一起分发，升级平台即可同步更新。")));
}

export function destroy() {}
