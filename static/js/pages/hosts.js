// pages/hosts.js —— 主机管理（FR-F ops-hosts）
// 必备：主机表格（状态 pill/分组/标签筛选）、详情抽屉（指标图 + 探活 + Agent 信息）、接入引导（install.sh 复制）
import { api, can } from "../api.js";
import { h, table, statusBadge, drawer, relTime, toast, select, copyText, busy, closeDrawer, reqLabel, needText, helpTip, rich, pager, fmtBytes } from "../ui.js";
import { showHostDetail } from "../host_detail.js";   // D41：详情抽屉抽为独立模块（大盘也用）


export async function render(root) {
  const state = { status: "", keyword: "" , page: 1, pageSize: 50 };
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  const head = h("div", { class: "page-head" },
    h("div", { class: "actions" },
      select([["", "全部状态"], ["up", "在线"], ["down", "离线"], ["pending", "待接入"],
       ["offline", "已下线"], ["removed", "已移出"]], "", {
        onchange: (e) => { state.status = e.target.value; state.page = 1; draw(); } }),
      // D22 同类修复：接入引导要调 write 接口（签发一次性安装令牌），
      // demo 点了必然 403 —— 与 rules/incidents 一致，按能力隐藏而不是让人点了再报错
      can("write") ? h("button", { class: "btn", onclick: (e) => import_guide(e.currentTarget) }, "接入新主机") : null));
  const body = h("div", { id: "hosts-body" });
  const pagerEl = h("div", { class: "pager" });   // D128
  root.replaceChildren(head, body);

  async function draw() {
    let hosts = [], d = { items: [], total: 0 };
    try { d = await api.get("/api/ops/hosts", { status: state.status, page: state.page, page_size: state.pageSize });
      hosts = d.items || []; }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, `加载失败：${e.message}`)); return; }
    if (state.keyword) {  // 关键字只过滤当前页（D128：主机量级小，生产上走后端过滤更稳）
      hosts = hosts.filter((x) => JSON.stringify(x).toLowerCase().includes(state.keyword.toLowerCase()));
    }
    // D62：列宽再平衡——主机名不再吞掉半屏；长 OS 串截断悬停可见；在线数随页头可见
    const upN = hosts.filter((x) => x.status === "up").length;
    body.replaceChildren(
      h("div", { class: "count-line muted" }, hosts.length ? `共 ${hosts.length} 台主机 · 在线 ${upN}` : ""),
      table([
      { title: "序号", width: "46px", render: (_r, i) => h("span", { class: "mono muted" }, String(i + 1)) },   // D129
      { title: "主机名", width: "240px", render: (r) => h("span", { class: "host-name", title: r.name }, r.name || "(未命名)") },
      { title: "状态", width: "86px", render: (r) => r.removed
      ? h("span", { class: "badge SKIP", title: "已移出：退役，Agent 停止上报，历史保留" }, "已移出")
      : r.offline ? h("span", { class: "badge WARN", title: "已下线：停止采集/告警，可恢复" }, "已下线")
      : statusBadge(r.status) },
      { title: "IP", width: "130px", render: (r) => h("span", { class: "mono" }, r.ip || "-") },
      { title: "OS", width: "170px", render: (r) => h("span", { class: "muted os-cell", title: r.os }, r.os || "-") },
      // D146：机器配置（几核几 G）——排障第一眼要看的就是规格
      { title: "配置", width: "132px", render: (r) => (r.cpu_cores || r.mem_total)
        ? h("span", { class: "mono", title: `${r.cpu_cores || "?"} 核 · ${fmtBytes(r.mem_total || 0)} 内存` },
            `${r.cpu_cores || "?"} 核 / ${fmtBytes(r.mem_total || 0)}`)
        : h("span", { class: "muted", title: "Agent 未上报硬件配置（升级 Agent 后可见）" }, "-") },
      { title: "Agent", width: "108px", render: (r) => {
          if (r.auth_state === "auth_failed")
            return h("span", { class: "badge FAIL", title: r.last_agent_error || "Agent 凭证失效" }, "认证失败");
          if (r.auth_state === "needs_reinstall")
            return h("span", { class: "badge WARN", title: "需要重新接入" }, "需重装");
          if (r.version_stale)
            return h("span", { class: "badge WARN",
              title: `当前 ${r.agent_version || "未知版本"}，可升级到 ${r.latest_agent_version || "最新版"}；点右侧「升级」生成命令` },
              "可升级");
          // D125：在线但 ring 无任何指标 = 静默失败（采集被跳过/配置错）——必须有眼可见的信号
          if (r.status === "up" && r.has_metrics === false)
            return h("span", { class: "badge WARN",
              title: "Agent 在线但平台未收到任何指标——采集可能被跳过或配置有误；展开详情看「最近错误」，必要时重装 Agent" },
              "无指标");
          return h("span", { class: "mono muted", title: `缓存待传 ${r.cache_depth || 0} 条` }, r.agent_version || "-");
        } },
      { title: "最近心跳", width: "100px", render: (r) => h("span", { class: "muted" }, relTime(r.last_beat)) },
      { title: "标识", width: "100px", render: (r) => h("span", { class: "mono muted", title: r.ident }, (r.ident || "").slice(0, 10)) },
      can("write") ? { title: "操作", width: "230px", render: (r) => h("div", { class: "row-actions" },
          r.version_stale ? h("button", { class: "btn ghost", title: "平台下发自升级任务，agent 下个心跳（≤15s）自动完成，无需登机",
            onclick: (e) => { e.stopPropagation(); directUpgrade(e.currentTarget, r); } }, "直接升级") : null,
          r.version_stale ? h("button", { class: "btn ghost", title: "生成升级命令（人工登机执行的兜底方式）",
            onclick: (e) => { e.stopPropagation(); upgradeGuide(e.currentTarget, r); } }, "命令") : null,
          h("button", { class: "btn ghost", title: r.offline ? "恢复上线：重新开放采集与告警，Agent 会自动续传本地缓存" : "下线：停止采集/告警；平台将拒收其上报（403），数据缓存在 Agent 本地，可随时恢复",
            onclick: (e) => { e.stopPropagation(); setOffline(r); } }, r.offline ? "恢复" : "下线"),
          h("button", { class: `btn ghost${r.removed ? "" : " danger"}`, title: r.removed ? "撤销移出（回到已下线待接入态）" : "移出退役：从列表隐藏、Agent 停止，历史告警/事件保留",
            onclick: (e) => { e.stopPropagation(); setRemove(r); } }, r.removed ? "撤移出" : "移出"),
          h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); renameHost(r); } }, "改名")) } : null,
    ], hosts, { emptyText: "暂无主机——点右上「接入新主机」查看安装命令",
      onRowClick: (r) => showHostDetail(r) }));
    body.append(pagerEl);   // D128：分页在列表右下
    pagerEl.replaceChildren(pager({ total: d.total || 0, page: state.page, pageSize: state.pageSize,
      onChange: (p, ps) => { state.page = p; if (ps !== state.pageSize) state.pageSize = ps; draw(); } }));
  }

  /* D121：平台侧一键升级——下发 self_upgrade 任务走 AgentTask 拉模式，
     agent 下个心跳（≤15s）领取并自动完成（下载→SHA256 校验→原子替换→自重启）。
     离线主机 409；旧 agent（不认识该任务）会回传失败——此时用旁边的「命令」按钮兜底。 */
  async function directUpgrade(btn, r) {
    try {
      const res = await busy(btn, "下发中…",
        () => api.post(`/api/ops/hosts/${r.ident}/upgrade`, {}));
      toast(res.data?.note || "升级任务已下发——agent 下个心跳自动执行", "ok");
      // 8s 后刷新看版本（agent 领任务+下载+重启约 10~20s；不阻塞用户）
      setTimeout(() => draw(), 8000);
    } catch (e) {
      if (String(e.message).includes("离线")) toast(`升级失败：${e.message}——可改用「命令」按钮生成升级命令`, "error");
      else toast(`升级下发失败：${e.message}`, "error");
    }
  }

  /* P1-12：版本漂移 → 一键生成升级命令。重跑安装命令会：
     ① 从平台下载最新 agent_main.py（并做 SHA256 校验）；
     ② 重启 launchd/systemd 到新版本；主机 ident/探活/历史数据不变。
     不做静默自动升级（Agent 以 root 跑，升级通道要可控、可审计）。 */
  async function upgradeGuide(btn, host) {
    try {
      const t = await busy(btn || document.activeElement, "生成令牌…",
        () => api.get("/api/ops/install-token"));
      const origin = location.origin;
      const cmd = `curl -fsSL ${origin}/install.sh | bash -s -- --server ${origin} --token ${t.install_token}`;
      drawer(`升级 Agent · ${host.name || host.ident.slice(0, 8)}`,
        h("div", { class: "note" },
          `把 Agent 从 ${host.agent_version || "未知版本"} 升级到 ${host.latest_agent_version || "最新版"}。`
          + "重跑安装命令会重启 Agent；主机标识、探活和历史数据不变。"),
        h("pre", { class: "wiz-cmd-body" }, cmd),
        h("div", { class: "detail-actions" },
          h("button", { class: "btn", onclick: () =>
            copyText(cmd, () => toast("升级命令已复制", "ok")) }, "复制命令")));
    } catch (e) { toast(`生成升级命令失败：${e.message}`, "error"); }
  }

  /* D56：主机改名——install.sh 自动起的名（host-xxxx / IP 形态 hostname）用户可改成自己认得的。
     只改显示名，ident（机器指纹）不变，Agent 凭证与数据不受影响。 */
  function renameHost(r) {
    const input = h("input", { class: "vs-input", value: r.name || "", placeholder: "1~64 个字符" });
    drawer(`主机改名 · ${r.name || r.ident.slice(0, 10)}`,
      h("div", { class: "note" }, "只改显示名，不影响 Agent 上报与历史数据。安装时打印的注册名也可在这里改。"),
      h("div", { class: "form-row" }, h("label", {}, reqLabel("主机名")), input),
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        // D59：空名先拦（服务端 422 同口径）
        const bad = needText(input.value, "主机名", 64);
        if (bad) { toast(bad, "error"); return; }
        try {
          await busy(e.currentTarget, "保存中…", () => api.post(`/api/ops/hosts/${r.ident}/rename`, { name: input.value.trim() }));
          toast("已改名", "ok"); closeDrawer(); draw();
        } catch (err) { toast(`改名失败：${err.message}`, "error"); }
      } }, "保存")));
  }

  /* 手动下线/恢复上线：下线置 status=down → 探活/告警/自愈自动跳过，Agent 上报被平台拒，
     可随时恢复。只改平台侧状态，不动 Agent 与历史数据。 */
  function setOffline(r) {
    const next = !r.offline;
    const go = () => api.post(`/api/ops/hosts/${r.ident}/offline`, { offline: next })
      .then(() => {
        toast(next ? "已下线（Agent 停止上报，数据缓存在本地，可随时恢复）"
                   : "已恢复上线：Agent 将自动续传本地缓存的数据", "ok");
        closeDrawer(); draw();
      })
      .catch((e) => { toast(`操作失败：${e.message}`, "error"); throw e; });
    // D155：下线是「用户按的」，但**后果**此前没说清——用户看到的是自己的主机
    // 不再上报、界面报 403，却不知道为什么。这里把几件事讲明白：
    //   ① 平台会主动拒收该主机的上报（403）；
    //   ② Agent 会把数据缓存在本地，不会丢；
    //   ③ 恢复上线后自动续传，界面的错误也会随之消失。
    if (!next) {
      // 恢复上线也不是"一键就完"——Agent 会自动续传本地缓存，
      // 界面上的错误要等下一次成功上报才消失。把这两件事告诉用户。
      const body = h("div", { class: "note" },
        h("div", {}, "恢复后这台主机重新向平台上报数据，探活、告警与自愈也会重新生效。"),
        h("div", { style: "margin-top:8px" }, "恢复后："),
        h("ul", { style: "margin:4px 0 0 18px" },
          h("li", {}, "Agent 会在下一次上报（默认 15 秒内）自动续传本地缓存的数据"),
          h("li", {}, "主机详情里的「当前错误」会在收到这次成功上报后自动消失"),
          h("li", {}, "下线期间没有任何上报，那段时间的指标补不回来——只能补 Agent 本地还留着的那部分")));
      drawer(`恢复上线 · ${r.name || r.ident.slice(0, 10)}`,
        body,
        h("div", { class: "detail-actions" },
          h("button", { class: "btn", onclick: async (e) => { await busy(e.currentTarget, "处理中…", go); closeDrawer(); } }, "确认恢复上线"),
          h("button", { class: "btn ghost", onclick: closeDrawer }, "取消")));
      return;
    }
    const body = h("div", {},
      h("div", { class: "note" },
        h("div", {}, "下线后这台主机将不再向平台上报数据，探活、告警与自愈也会停止。"),
        h("div", { style: "margin-top:8px" }, "会发生什么："),
        h("ul", { style: "margin:4px 0 0 18px" },
          h("li", {}, "平台会主动拒收它的上报（Agent 收到 403），这是预期行为、不是故障"),
          h("li", {}, "Agent 会把数据缓存在本地（不会丢），超过缓存上限后丢弃最旧的"),
          h("li", {}, "恢复上线后，Agent 自动续传这段时间的缓存数据"),
          h("li", {}, "主机详情里的「当前错误」会显示 403，直到恢复后下一次成功上报才消失"))),
      h("div", { class: "note", style: "margin-top:10px" },
        "记录、历史告警与巡检数据都会保留，随时可以恢复上线。"));
    drawer(`下线主机 · ${r.name || r.ident.slice(0, 10)}`,
      body,
      h("div", { class: "detail-actions" },
        h("button", { class: "btn", onclick: async (e) => { await busy(e.currentTarget, "处理中…", go); closeDrawer(); } }, "确认下线"),
        h("button", { class: "btn ghost", onclick: closeDrawer }, "取消")));
  }

  /* 手动移出/撤移出：移出=退役（含下线）→ 从主机列表隐藏、Agent 停止上报且需重新签发
     令牌才能复原；历史告警/事件/巡检保留（审计留痕，不做物理级联删除）。走强确认。 */
  function setRemove(r) {
    const next = !r.removed;
    const go = () => api.post(`/api/ops/hosts/${r.ident}/remove`, { removed: next })
      .then(() => { toast(next ? "已移出（退役）" : "已撤销移出", "ok"); closeDrawer(); draw(); })
      .catch((e) => { toast(`操作失败：${e.message}`, "error"); throw e; });
    const removeBody = next
      ? h("div", {},
          h("div", {}, "确认移出该主机？移出（= 退役）后将："),
          h("ul", { style: "margin:4px 0 0 18px" },
            h("li", { html: rich("从主机列表隐藏（历史告警/事件/巡检**保留**，审计留痕）") }),
            h("li", {}, "平台拒收它的上报（Agent 收到 403）——数据缓存在 Agent 本地，不会丢"),
            h("li", { html: rich("需要**重新接入**（重签发安装令牌、重跑安装命令）才能复原；"
              + "撤销移出只会回到「已下线」态，仍需重新接入") })))
      : h("div", {},
          h("div", {}, "撤销移出：主机回到「已下线」待接入态。"),
          h("ul", { style: "margin:4px 0 0 18px" },
            h("li", {}, "平台会重新接受它的上报"),
            h("li", {}, "但要真正恢复采集，仍需重新接入（重签发安装令牌、重跑安装命令）")));
    drawer(`移出主机 · ${r.name || r.ident.slice(0, 10)}`,
      h("div", { class: "note" }, removeBody),
      h("div", { class: "detail-actions" },
        h("button", { class: "btn danger", onclick: async (e) => { await busy(e.currentTarget, "处理中…", go); closeDrawer(); } }, next ? "确认移出" : "撤销移出"),
        h("button", { class: "btn ghost", onclick: closeDrawer }, "取消")));
  }

  async function import_guide(btn) {
    let tok = "";
    try {
      // D64：令牌是一次性的，双击会铸出两个（浪费一个）→ 生成期间禁用按钮
      const t = await busy(btn || document.activeElement, "生成令牌…", () => api.get("/api/ops/install-token"));
      tok = t.install_token;
    }
    catch (e) { toast(`生成安装令牌失败：${e.message}`, "error"); return; }
    // 用真实 origin（D15 审计修复）：原先写死字面占位符 `http://<平台地址>`，
    // 买家点「复制」粘到目标机会直接报 `Could not resolve host: <平台地址>`——
    // 手册说"复制这条命令到目标机执行"，复制出来却跑不起来。
    const origin = location.origin;

    /* D57：探活项（用户要求「默认带上探活」）。默认给一条 SSH 端口探活（所有 Linux/Mac 都有
       sshd，一定探得活）；可增删改。只把"三项都填了"的行拼进 --probes。
       值里剥掉单引号（命令用单引号包 JSON，防止破坏 shell 引号）。 */
    const probeRows = [{ name: "SSH 端口", type: "tcp", target: "127.0.0.1:22" }];
    const PROBE_PH = { tcp: "127.0.0.1:22", http: "http://127.0.0.1:8080/health",
      process: "进程名关键字（如 nginx）", "https-cert": "https://your.domain" };
    const safe = (v) => String(v || "").replace(/'/g, "").trim();
    const probesJson = () => {
      const items = probeRows.filter((r) => safe(r.name) && safe(r.target))
        .map((r) => ({ name: safe(r.name), type: r.type, target: safe(r.target) }));
      return items.length ? JSON.stringify(items) : "";
    };
    // 命令用 <pre> 而非 <input>：长命令（尤其带 --probes 的 JSON）在窄输入框里会被截断，
    // 用户复制到手的是半条命令（D15 同类的"复制出来跑不起来"）。改成整块可换行的代码块。
    const cmdBox = h("pre", { class: "wiz-cmd-body" });
    const rebuildCmd = () => {
      const pj = probesJson();
      cmdBox.textContent = `curl -fsSL ${origin}/install.sh | bash -s -- --server ${origin} --token ${tok}`
        + (pj ? ` --probes '${pj}'` : "");
    };

    const probeTable = h("div", { class: "wiz-probes" });
    const redrawProbes = () => {
      probeTable.replaceChildren(
        h("div", { class: "wiz-probe-head" },
          h("span", {}, "名称"), h("span", {}, "类型"), h("span", {}, "目标"), h("span", {})),
        ...probeRows.map((r, i) => h("div", { class: "wiz-probe-row" },
          h("input", { class: "vs-input", value: r.name, placeholder: "如：业务HTTP",
            oninput: (e) => { r.name = e.target.value; rebuildCmd(); } }),
          h("select", { class: "vs-input", onchange: (e) => { r.type = e.target.value; rebuildCmd(); redrawProbes(); } },
            ...["tcp", "http", "process", "https-cert"].map((t) => h("option", { value: t, selected: r.type === t ? "" : null }, t))),
          h("input", { class: "vs-input mono", value: r.target, placeholder: PROBE_PH[r.type] || "",
            oninput: (e) => { r.target = e.target.value; rebuildCmd(); } }),
          h("button", { class: "btn ghost sm danger", title: "删除这一条探活",
            onclick: () => { probeRows.splice(i, 1); rebuildCmd(); redrawProbes(); } }, "删"))));
    };
    const addProbeRow = () => { probeRows.push({ name: "", type: "tcp", target: "" }); redrawProbes(); };

    // 初始化必须画一次：否则"默认带一条 SSH 探活"只在内存里、界面上是空的
    // （用户实测截图里探活区只有「+ 添加探活项」，点一下才会把默认行带出来）。
    redrawProbes();
    rebuildCmd();

    /* 两步走：① 先自检（只读）② 通过后再安装。布局按"看得清、不拥挤"重排：
       抽屉加宽（CSS `.detail-panel:has(.wiz)`）、去掉多层嵌套卡片、命令改成整块可换行的代码块、
       探活列表给表头与栅格列；说明文字只在必要处出现（原「前置条件」表与自检项重复，已合并）。
       结构先分段建好再组装——嵌套十层的 JSX 式写法既难读也容易把括号写错。 */
    const os = { cur: /Mac|iPhone|iPad/.test(navigator.userAgent) ? "macos" : "linux" };
    const preflightCmd = () => `curl -fsSL ${origin}/preflight_${os.cur}.sh | bash -s -- --server ${origin}`;
    const pfBody = h("pre", { class: "wiz-cmd-body" }, preflightCmd());
    const osSeg = h("div", { class: "seg-row big" }, ...["macos", "linux"].map((k) =>
      h("button", {
        class: `btn ghost${os.cur === k ? " active" : ""}`,
        onclick: (e) => {
          os.cur = k;
          pfBody.textContent = preflightCmd();
          [...e.currentTarget.parentElement.children].forEach((b, i) =>
            b.classList.toggle("active", i === (k === "macos" ? 0 : 1)));
        },
      }, k === "macos" ? "macOS" : "Linux")));

    const pfItems = [
      ["操作系统", "是否为目标系统（两个脚本各自会拦错系统，并直接给出正确的那个命令）"],
      ["Python", "≥ 3.9（推荐 3.10+），并验证 ssl / sqlite3 等标准库可用"],
      ["网络", "能否**出站**访问平台；并明确**无需开放任何入站端口**"],
      ["时间同步", "与平台的时差（偏差过大会阻断：时间错会污染取证时间窗与告警时刻）"],
      ["资源与权限", "磁盘余量 / 可用内存 / HOME 可写 / root 或 sudo"],
      ["服务托管", "launchd（macOS）/ systemd（Linux）；缺失会如实告知将降级为常驻进程"],
      ["依赖", "psutil / httpx 由安装脚本自动安装，无需预装"],
      ["既有安装", "是否已装过 Agent（会说明这是升级，并提示将重启到新版本）"],
    ];

    const stepHead = (n, title, tag, tagCls) => h("div", { class: "wiz-step-head" },
      h("span", { class: "wiz-badge" }, String(n)),
      h("div", { class: "wiz-step-title" }, title,
        h("span", { class: `wiz-tag${tagCls ? " " + tagCls : ""}` }, tag)));

    const cmdBlock = (label, body, copyValue) => h("div", { class: "wiz-cmd" },
      h("div", { class: "wiz-cmd-head" },
        h("span", { class: "wiz-cmd-label" }, label),
        h("button", { class: "btn secondary sm", onclick: (e) => copy(copyValue(), e.target) }, "复制")),
      body);

    const hint = (text, cls) => h("div", { class: `wiz-hint${cls ? " " + cls : ""}`, html: rich(text) });

    const checkList = h("div", { class: "wiz-checks" }, ...pfItems.map(([k, v]) =>
      h("div", { class: "wiz-check" },
        h("span", { class: "wiz-check-k" }, k),
        h("span", { class: "wiz-check-v", html: rich(v) }))));

    const probeField = h("div", { class: "wiz-field" },
      h("label", {}, "服务探活", helpTip("接入后顺带盯的对象：tcp=端口能否连通；http=接口是否 200；"
        + "process=进程是否存在；https-cert=证书剩余天数（可做「30 天内到期」告警）。"
        + "探活失败会像指标越阈一样触发告警。")),
      probeTable,
      h("div", { class: "wiz-probe-foot" },
        h("button", { class: "btn ghost sm", onclick: addProbeRow }, "+ 添加探活项"),
        h("span", { class: "wiz-hint inline" }, "默认盯本机 SSH 端口（所有 Linux / Mac 都有 sshd）")));

    const tokenRow = h("div", { class: "wiz-token" },
      h("span", { class: "wiz-token-k" }, "安装令牌"),
      h("code", { class: "wiz-token-v" }, tok),
      h("button", { class: "btn secondary sm", onclick: (e) => copy(tok, e.target) }, "复制"));

    const step1 = h("section", { class: "wiz-step" },
      stepHead(1, "自检目标机环境", "只读 · 不改目标机"),
      h("div", { class: "wiz-field" },
        h("label", {}, "目标机系统", helpTip("决定给你哪条命令：macOS 用 launchd、Linux 用 systemd。"
          + "选错没关系——两个自检脚本各自会拦错系统并指向正确的那个。")),
        osSeg),
      cmdBlock("自检命令（粘到目标机执行，约 5~10 秒）", pfBody, preflightCmd),
      checkList,
      hint("退出码：**0** = 可以安装　·　**1** = 有阻断项（照输出提示处理后再跑一次）　·　**2** = 系统用错"));

    const step2 = h("section", { class: "wiz-step" },
      stepHead(2, "自检通过后安装", "写入 Agent 并注册到平台", "muted"),
      tokenRow,
      hint("令牌**一次性**，仅用于首次注册；已注册的机器重装 / 升级**免令牌**"
        + "（重跑安装命令即升级，并重启到新版本）。"),
      probeField,
      cmdBlock("一键安装命令", cmdBox, () => cmdBox.textContent),
      hint("执行后约 15 秒，本机出现在上方主机列表（状态**在线**）；"
        + "探活失败会像指标越阈一样触发告警。"));

    drawer("接入新主机",
      h("div", { class: "wiz" },
        h("div", { class: "wiz-lead", html: rich("**两步走**：先自检（只读，不改目标机任何东西）"
          + "→ 结论通过后再安装。自检会告诉你差什么、怎么补；**装不装由你决定**。") }),
        step1,
        step2));
  }

  await draw();
}

function copy(text, btn) {
  // D28：统一走带降级的 copyText（HTTP 公网下 navigator.clipboard 不可用）
  copyText(text, () => { const t = btn.textContent; btn.textContent = "已复制"; setTimeout(() => (btn.textContent = t), 1200); });
}

export function destroy() { /* 详情图表由 host_detail.js 自管 */ }
