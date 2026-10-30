// pages/settings.js —— 系统设置（FR-F ops-settings）
// 必备：LLM 配置（CRUD + 连接测试 + 用量）、License 激活（引导 + 状态）、
//       通知渠道配置（CRUD + 连接测试）、基础项（主题/时区/Agent 安装令牌）
import { api, getUser, can } from "../api.js";
import { h, table, badge, drawer, toast, fmtTime, select, note, copyText, precheckCard, busy, reqLabel, needText, needUrl, statusBadge, fill, helpTip } from "../ui.js";

// D175-3：后端 purpose 枚举 → 用户看得懂的中文（用户反馈"按 purpose 是啥意思，不要有英文描述"）。
// 未收录的枚举回落显示原值——宁可显示内部码，也不把这一行悄悄吞掉。
const PURPOSE_ZH = {
  rca: "根因诊断（事件/告警的一键诊断）",
  chat: "AI 助手对话",
  summary: "摘要与对话历史压缩",
  inspect: "巡检报告生成",
  fixcheck: "修复建议校验",
  embed: "案例向量检索（嵌入）",
  suggest: "AI 建议（阈值推荐等）",
};

export async function render(root) {
  const me = getUser() || {};
  const canWrite = can("write");       // LLM/渠道/License：admin 专属
  root.replaceChildren(h("div", { class: "empty" }, "加载中…"));

  const tabs = h("div", { class: "tabs" });
  const body = h("div", { id: "settings-body" });
  const HEAD = [["llm", "LLM 配置"], ["notify", "通知渠道"], ["license", "License"], ["basic", "基础项"],
                ...(me.role === "admin" ? [["eval", "诊断评测"]] : [])];   // FR-428：admin 专属
  let cur = "llm";
  HEAD.forEach(([k, t]) => tabs.append(h("button", { class: "tab" + (k === cur ? " active" : ""), onclick: () => { cur = k; renderTabs(); draw(); } }, t)));
  function renderTabs() { [...tabs.children].forEach((b, i) => b.classList.toggle("active", HEAD[i][0] === cur)); }

  root.replaceChildren(h("div", { class: "page-head" }, h("span", { class: "count-pill" }, me.role || "-")), tabs, body);

  async function draw() {
    body.replaceChildren(h("div", { class: "empty" }, "加载中…"));
    if (cur === "llm") return drawLLM();
    if (cur === "notify") return drawNotify();
    if (cur === "license") return drawLicense();
    if (cur === "eval") return drawEval();
    return drawBasic();
  }

  // ---------- LLM ----------
  async function drawLLM() {
    let cfgs = [], usage = [], quota = null;
    try { cfgs = await api.get("/api/llm-configs"); } catch (e) { body.replaceChildren(h("div", { class: "empty" }, e.message)); return; }
    try { const u = await api.get("/api/llm-configs/usage"); usage = u.items || []; quota = u.quota || null; } catch { /* ignore */ }
    // D175-3：用途是后端枚举（rca/chat/summary/…），**直接摆给用户看不懂**
    // （用户反馈："按 purpose 是啥意思？？不要有英文描述"）——这里映射成中文并按消耗排序。
    const byPurpose = {};
    (usage || []).forEach((u) => {
      const k = PURPOSE_ZH[u.purpose] || u.purpose;      // 未知枚举回落原值，不吞掉
      byPurpose[k] = (byPurpose[k] || 0) + Number(u.total_tokens || 0);
    });
    const purposeRows = Object.entries(byPurpose)
      .map(([k, v]) => ({ k, v })).sort((a, b) => b.v - a.v);
    fill(body,
      note("API 密钥加密存储，页面只显示掩码。" +
        "对话/诊断与向量检索可能用不同服务商的接口，网关已自动适配常见的两种返回格式。"),
      canWrite ? h("div", { class: "actions", style: "margin-bottom:10px" }, h("button", { class: "btn", onclick: () => llmEditor(null) }, "新增模型配置")) : null,
      table([
        { title: "名称", render: (r) => r.name },
        { title: "Provider", width: "100px", render: (r) => h("span", { class: "muted" }, r.provider || "-") },
        { title: "Base URL", render: (r) => h("span", { class: "mono", style: "font-size:0.76rem" }, r.base_url || "(默认)") },
        { title: "主模型", width: "150px", render: (r) => h("span", { class: "mono" }, r.model) },
        { title: "快模型", width: "150px", render: (r) => h("span", { class: "mono muted" }, r.fast_model || "-") },
        { title: "默认", width: "66px", render: (r) => r.is_default ? badge("是", "PASS") : "" },
        // D163-3：降级通道由用户显式指定，列表里要一眼看出哪套配置在兜底
        { title: "降级通道", width: "96px", render: (r) => r.fallback_enabled ? badge("是", "PASS") : h("span", { class: "muted" }, "-") },
        { title: "启用", width: "66px", render: (r) => badge(r.enabled ? "启用" : "停用", r.enabled ? "PASS" : "SKIP") },
        { title: "操作", width: "170px", render: (r) => h("div", { class: "row-actions" },
            h("button", { class: "btn ghost", onclick: async (e) => { e.stopPropagation();
              // D51：模型连通测试要真的打一次模型，10~20s → 必须有中间态（含已用秒数）
              try { const t = await busy(e.currentTarget, "测试中…", () => api.post(`/api/llm-configs/${r.id}/test`));
                toast(t.ok ? `连通：${t.detail || "ok"}` : `不通：${t.detail}`, t.ok ? "ok" : "error"); }
              catch (err) { toast(`测试失败：${err.message}`, "error"); } } }, "测试"),
            canWrite ? h("button", { class: "btn ghost", onclick: (e) => { e.stopPropagation(); llmEditor(r); } }, "编辑") : null,
            // D147：模型配置此前只能增/改，换了供应商旧配置删不掉（用户实测：
            // 要换通义千问，MiniMax 那套无处可删）。
            canWrite ? h("button", { class: "btn ghost danger", onclick: async (e) => {
              e.stopPropagation();
              if (!window.confirm(`删除模型配置「${r.name}」？删除后该配置的 API Key 一并移除${r.is_default ? "（会按顺序把下一套启用配置升为默认）" : ""}。`)) return;
              try {
                const res = await busy(e.currentTarget, "删除中…", () => api.del(`/api/llm-configs/${r.id}`));
                toast(res.promoted_default ? "已删除，已自动切换默认模型" : "已删除", "ok"); draw();
              } catch (err) { toast(`删除失败：${err.message}`, "error"); }
            } }, "删除") : null) },
      ], cfgs, { emptyText: "尚未配置模型——将回落到 .env 兜底配置" }),
      !quota ? null : h("div", { class: "card", style: "margin-top:14px" },
        h("h3", {}, "今日 token 配额（成本闸门）"),
        quota.limit
          ? h("div", {},
              h("div", { class: "muted" }, `额度来源：${quota.scope === "demo" ? "演示模式限额" : "全局限额"}　重置：${fmtTime(quota.resets_at)}`),
              h("div", { class: quota.exceeded ? "note" : "", style: "margin-top:6px" },
                quota.exceeded
                  ? `今日额度已用尽（${Number(quota.used).toLocaleString()} / ${Number(quota.limit).toLocaleString()}）——AI 功能将暂停，次日自动重置`
                  : `已用 ${Number(quota.used).toLocaleString()} / ${Number(quota.limit).toLocaleString()}（剩余 ${Number(quota.remaining).toLocaleString()}）`))
          : h("div", { class: "muted" }, "未设每日限额。若平台对外开放，建议设一个每日上限，防止 token 被恶意消耗（管理员可在服务端配置中设置）。")),
      h("div", { class: "card", style: "margin-top:14px" },
        h("h3", {}, "近 7 日 token 用量"),
        h("div", { class: "muted", style: "font-size:0.78rem;margin-bottom:8px" },
          "按功能分开统计，便于判断钱花在哪：根因诊断最耗 token，摘要与向量检索很少。"),
        Object.keys(byPurpose).length ? table([
          { title: "用在哪儿", render: (r) => h("span", {}, r.k) },
          { title: "消耗 token", width: "150px", render: (r) => h("span", { class: "mono" }, r.v.toLocaleString()) },
          { title: "占比", width: "220px", render: (r) => {
              const total = purposeRows.reduce((a, x) => a + x.v, 0) || 1;
              const pct = Math.round((r.v / total) * 100);
              return h("span", { class: "muted" }, `${pct}%`);
            } },
        ], purposeRows, {}) : h("div", { class: "empty" }, "暂无用量记录")));
  }

  function llmEditor(c) {
    const name = h("input", { class: "vs-input", value: c?.name || "main-minimax" });
    const provider = h("input", { class: "vs-input mono", value: c?.provider || "openai-compatible" });
    const base = h("input", { class: "vs-input mono", value: c?.base_url || "", placeholder: "https://api.example.com/v1（OpenAI 兼容地址）" });
    const key = h("input", { class: "vs-input", type: "password", placeholder: "sk-…（留空=不修改）" });
    const model = h("input", { class: "vs-input mono", value: c?.model || "", placeholder: "如 qwen3.8-max / deepseek-chat" });
    const fast = h("input", { class: "vs-input mono", value: c?.fast_model || "", placeholder: "留空 = 与主模型相同" });
    const emb = h("input", { class: "vs-input mono", value: c?.extra_config?.embedding_model || "", placeholder: "留空 = 不启用向量检索" });
    const def = select([["1", "是"], ["0", "否"]], c?.is_default ? "1" : "0");
    // D163-3：降级通道 = 主模型因服务商额度/限流不可用时改用哪套配置（用户显式指定）
    const fb = select([["0", "否"], ["1", "是"]], c?.fallback_enabled ? "1" : "0");
    drawer(c ? `编辑模型配置 · ${c.name}` : "新增模型配置",
      h("div", { class: "note" }, "主模型用于诊断/收敛等推理，快模型用于摘要与历史压缩（两种角色按精度与速度分级）。"
        + "主模型因服务商额度耗尽而不可用时，平台会改用**勾了「作为降级通道」**的配置继续服务——平台不会擅自用快模型兜底。"),
      h("div", { class: "grid-2" },
        frow(reqLabel("名称"), name), frow("Provider 标识", provider),
        frow("Base URL", base), frow("API Key", key),
        frow([reqLabel("主模型"), helpTip("重任务用（根因诊断、报告撰写、因果判定）——质量优先，可以贵一点。")], model), frow(["快模型", helpTip("轻任务用（会话摘要、历史压缩、推荐问题生成）——速度/成本优先，反复调用的地方都用它。")], fast),
        frow(["Embedding 模型", helpTip("案例库向量检索用：换个说法描述同一类故障也能召回历史案例（如「安卓握手失败」能命中「证书链不完整」）。未配置时案例检索会降级为关键词匹配。")], emb), frow(["设为默认", helpTip("新建诊断默认用这套配置；同用途可配多套，随时切换。")], def),
        frow(["作为降级通道", helpTip("主模型因服务商额度耗尽或限流而不可用时，平台改用这套配置继续服务（可与主模型不同服务商/不同模型）。**只有勾了这里的配置才会被用作降级**——快模型只用于轻量任务，平台不会擅自拿它兜底。")], fb)),
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        // D59：名称/主模型是刚需（服务端同步校验 + 重名 400），客户端先拦
        const bad = needText(name.value, "配置名称", 64) || needText(model.value, "主模型", 64) || needUrl(base.value, "Base URL");
        if (bad) { toast(bad, "error"); return; }
        const payload = { name: name.value.trim(), provider: provider.value, base_url: base.value.trim(), api_key: key.value,
          model: model.value.trim(), fast_model: fast.value.trim(), extra_config: { embedding_model: emb.value }, enabled: true, is_default: def.value === "1",
          fallback_enabled: fb.value === "1" };
        try {
          await busy(e.currentTarget, "保存中…", () => c
            ? api.put(`/api/llm-configs/${c.id}`, payload)
            : api.post("/api/llm-configs", payload));
          toast("已保存", "ok"); close(); draw();
        } catch (err) { toast(`保存失败：${err.message}`, "error"); }
      } }, "保存")));
  }

  // ---------- 通知渠道 ----------
  async function drawNotify() {
    let items = [];
    try { items = (await api.get("/api/ops/notify-channels")).items || []; } catch (e) { body.replaceChildren(h("div", { class: "empty" }, e.message)); return; }
    fill(body,
      h("div", { class: "note" }, "内置飞书 / 钉钉 / 企业微信 / 邮件 / 通用 Webhook / 阿里云语音 六类友好表单；" +
        "若需接入其它渠道（Slack / Telegram 等），可在配置里直接填 apprise_url 兼容 150+ 服务。"),
      canWrite ? h("div", { class: "actions", style: "margin-bottom:10px" }, h("button", { class: "btn", onclick: () => chanEditor() }, "新增渠道")) : null,
      table([
        { title: "名称", render: (r) => r.name },
        { title: "类型", width: "100px", render: (r) => badge(r.type, "ACCENT") },
        { title: "连通", width: "86px", render: (r) => statusBadge(r.last_test_status) },
        { title: "启用", width: "70px", render: (r) => badge(r.enabled ? "启用" : "停用", r.enabled ? "PASS" : "SKIP") },
        { title: "操作", width: "190px", render: (r) => h("div", { class: "row-actions" },
            h("button", { class: "btn ghost", onclick: async (e) => {
              // D51：真发一条通知出去（受渠道网络影响，可能几秒）→ 必须有中间态
              // D153：阿里云语音的「测试」= **真打一通电话**（不是探活）→ 明确告知，
              // 否则用户点一下自己的手机就响了，会以为平台在骚扰。
              if (r.type === "aliyun_voice") {
                const ph = r.config_json?.phone || "该号码";
                if (!window.confirm(`即将向 ${ph} 拨打一通测试电话（会真的响）。\n确定继续吗？`)) return;
              }
              try {
                const t = await busy(e.currentTarget, "发送中…", () => api.post(`/api/ops/notify-channels/${r.id}/test`));
                toast(t.ok ? "渠道连通" : `发送失败：${t.detail}`, t.ok ? "ok" : "error");
              } catch (err) { toast(`测试失败：${err.message}`, "error"); }
              draw(); } }, "测试"),
            canWrite ? h("button", { class: "btn ghost", onclick: () => chanEditor(r) }, "编辑") : null,
            canWrite ? h("button", { class: "btn ghost", onclick: async (e) => {
              try { await busy(e.currentTarget, "删除中…", () => api.del(`/api/ops/notify-channels/${r.id}`)); toast("已删除", "ok"); }
              catch (err) { toast(`删除失败：${err.message}`, "error"); }
              draw(); } }, "删除") : null) },
      ], items, { emptyText: "尚未配置通知渠道（告警仍会落库，只是不发通知）" }));
  }

  function chanEditor(c) {
    // D59：支持编辑（原先只有建/删——名字打错、webhook 换地址都只能删了重建）。
    // 出参里 webhook/apprise 是掩码，所以编辑框留空 = 保持不变（placeholder 显示掩码尾）。
    const editing = !!c;
    const cfg = c?.config_json || {};
    const name = h("input", { class: "vs-input", value: c?.name || "", placeholder: "值班群-webhook" });
    const type = select([["feishu", "飞书"], ["dingtalk", "钉钉"], ["wecom", "企业微信（微信群机器人）"],
      ["phone", "电话（S1 紧急专用）"], ["email", "邮件"], ["webhook", "通用 Webhook"],
      ["aliyun_voice", "阿里云语音（S1 紧急专用）"]], c?.type || "webhook");
    const url = h("input", { class: "vs-input mono", placeholder: cfg.webhook_url ? `留空 = 保持不变（现 ${cfg.webhook_url}）` : "https://open.feishu.cn/open-apis/bot/v2/hook/xxx 或 oapi.dingtalk.com/robot/send?access_token=…" });
    const secret = h("input", { class: "vs-input mono", placeholder: cfg.secret ? "留空 = 保持不变（已配置）" : "钉钉加签 secret（可选）" });
    const apprise = h("input", { class: "vs-input mono", placeholder: cfg.apprise_url ? `留空 = 保持不变（现 ${cfg.apprise_url}）` : "apprise_url 逃生口（填了则优先，可接 150+ 渠道）" });
    const smtp = h("input", { class: "vs-input mono", placeholder: cfg.smtp?.host ? "留空 = 保持不变（已配置 SMTP）" : "邮件：smtp.exmail.qq.com:465:user@x.com:pwd:to@x.com" });
    // D144：电话渠道必须有手机号输入。语音网关仍走 apprise_url，支持 `{phone}` 占位：
    //   twilio://账号:令牌@主叫号/{phone} → 后端在投递前替换为上面填的被叫号。
    const phone = h("input", { class: "vs-input mono", value: cfg.phone || "",
      placeholder: "+8613800138000（E.164 格式，S1 直接拨打此号）" });
    const voice = h("input", { class: "vs-input mono",
      placeholder: cfg.apprise_url ? `留空 = 保持不变（现 ${cfg.apprise_url}）` : "twilio://账号:令牌@主叫号/{phone}（{phone} 会替换为上面的手机号）" });

    // D152：阿里云语音。用户在阿里云开通语音服务后，把 AccessKey + 语音模板 Code
    // 填进来，平台用 RPC 签名直连 dyvmsapi.aliyuncs.com 发起 TTS 呼叫。
    // AccessKey Secret 出参是掩码（"***"），编辑时留空 = 保持原值（同 webhook 语义）。
    const akId = h("input", { class: "vs-input mono", value: cfg.access_key_id || "",
      placeholder: "阿里云 AccessKey ID（如 LTAI5t…）" });
    const akSec = h("input", { class: "vs-input mono",
      placeholder: cfg.access_key_secret ? "留空 = 保持不变（已配置）" : "阿里云 AccessKey Secret" });
    const ttsCode = h("input", { class: "vs-input mono", value: cfg.tts_code || "",
      placeholder: "语音通知模板 Code（如 TTS_123456789，在阿里云「语音服务 → 通知模板」查看）" });
    // 模板变量名：TtsParam 的 key 必须与模板正文里的变量名一致，否则播的是模板固定文案
    const ttsKey = h("input", { class: "vs-input mono", value: cfg.tts_param_key || "",
      placeholder: "模板变量名（可选，默认 content——与模板正文里的变量名一致）" });
    const showNum = h("input", { class: "vs-input mono", value: cfg.called_show_number || "",
      placeholder: "主叫显示号码（可选，通用模式可留空；专用模式填已备案的真实号）" });
    const voicePhone = h("input", { class: "vs-input mono", value: cfg.phone || "",
      placeholder: "13800138000（多个号码用逗号分隔：138…,139…）" });
    // D165：被叫支持多个——逗号/分号分隔，逐个拨打（值班组场景）
    // 播放参数（官方 SingleCallByTts 支持；默认由阿里云决定：播 3 遍 / 音量 100 / 语速默认）
    const playTimes = h("input", { class: "vs-input mono", value: cfg.play_times ?? "",
      placeholder: "播放次数（可选，1~3，默认 3）" });
    const volume = h("input", { class: "vs-input mono", value: cfg.volume ?? "",
      placeholder: "音量（可选，0~100，默认 100）" });
    const speed = h("input", { class: "vs-input mono", value: cfg.speed ?? "",
      placeholder: "语速（可选，-500~500，0 为默认）" });
    const outId = h("input", { class: "vs-input mono", value: cfg.out_id || "",
      placeholder: "外部流水号（可选，≤15 字节；阿里云回执里会带回，可用来筛这批呼叫）" });

    // 按类型只显示相关字段（此前所有字段无条件铺开：选“电话”却看不到手机号——D144）
    const rowUrl = frow("Webhook 地址（飞书 / 钉钉 / 企业微信 / 通用）", url);
    const rowSecret = frow("钉钉加签 secret（可选）", secret);
    const rowSmtp = frow("邮件 SMTP 串（user:pwd 会被加密存储）", smtp);
    const rowPhone = frow(reqLabel("手机号（E.164）"), phone);
    const rowVoice = frow(reqLabel("语音网关（apprise_url）"), voice);
    const rowApprise = frow("apprise_url 逃生口（可选，优先使用）", apprise);
    const rowAkId = frow(reqLabel("AccessKey ID"), akId);
    const rowAkSec = frow(reqLabel("AccessKey Secret"), akSec);
    const rowTts = frow(reqLabel("语音模板 Code"), ttsCode);
    const rowTtsKey = frow("模板变量名（可选，默认 content）", ttsKey);
    const rowShowNum = frow("主叫显示号码（可选）", showNum);
    const rowVoicePhone = frow([reqLabel("被叫手机号"),
      helpTip("支持**多个号码**：用逗号或分号分隔（如 13800001111,13900002222），告警时逐个拨打；"
        + "国内号直接填 11 位，国际号 +86 开头。注意阿里云对同一号码有流控（1 分钟 1 次/1 小时 5 次/24 小时 20 次），"
        + "不同号码互不影响。")], voicePhone);
    const rowPlayTimes = frow("播放次数（可选，1~3）", playTimes);
    const rowVolume = frow("音量（可选，0~100）", volume);
    const rowSpeed = frow("语速（可选，-500~500）", speed);
    const rowOutId = frow("外部流水号（可选，≤15 字节）", outId);
    const hint = h("div", { class: "note" });
    function syncType() {
      const t = type.value;
      const isPhone = t === "phone";
      const isAli = t === "aliyun_voice";
      rowUrl.hidden = isPhone || isAli || t === "email";
      rowSecret.hidden = t !== "dingtalk";
      rowSmtp.hidden = t !== "email";
      rowPhone.hidden = !isPhone;
      rowVoice.hidden = !isPhone;
      rowApprise.hidden = isPhone || isAli || t === "email";
      rowAkId.hidden = !isAli;
      rowAkSec.hidden = !isAli;
      rowTts.hidden = !isAli;
      rowTtsKey.hidden = !isAli;
      rowShowNum.hidden = !isAli;
      rowVoicePhone.hidden = !isAli;
      rowPlayTimes.hidden = !isAli;
      rowVolume.hidden = !isAli;
      rowSpeed.hidden = !isAli;
      rowOutId.hidden = !isAli;
      if (isPhone) {
        hint.replaceChildren(
          "S1 紧急告警走电话语音：填被叫手机号 + 语音网关地址。网关地址里写 ",
          h("code", {}, "{phone}"),
          " 即可复用同一网关、只改被叫号（如 ",
          h("code", {}, "twilio://SID:TOKEN@+8613800000000/{phone}"),
          "）；也可直接填含真实被叫号的完整 apprise_url。配好后务必点「测试」确认能打通。");
      } else if (isAli) {
        hint.replaceChildren(
          "阿里云语音：平台用你填的 AccessKey 做 RPC 签名，直连 ",
          h("code", {}, "dyvmsapi.aliyuncs.com"),
          " 发起 TTS 呼叫（Action=SingleCallByTts）。① AccessKey 需有 ",
          h("b", {}, "语音服务"),
          " 的呼叫权限：",
          "在 RAM 里给它挂「语音服务」的系统策略，或自定义策略放行 ",
          h("code", {}, "dyvms:SingleCallByTts"),
          "；② 语音模板 Code 在阿里云「语音服务 → 通知模板」创建，模板正文里要留一个变量",
          "（默认叫 ",
          h("code", {}, "content"),
          "，可在下面「模板变量名」改成你实际用的名字）——平台把告警内容塞进这个变量后播报",
          "（模板里没这个变量就只会念模板固定文案）；③ 被叫手机号：国内 ",
          h("code", {}, "13800138000"),
          "，国际 ",
          h("code", {}, "+8613800138000"),
          "。",
          "配好后点「测试」会真实呼叫一次该号码，确认能打通再用于 S1 告警。");
      } else if (t === "email") {
        hint.replaceChildren("邮件走 SMTP：按 主机:端口:账号:密码:收件地址 五段冒号填写。密码会加密存储。");
      } else {
        hint.replaceChildren("粘贴机器人 webhook 地址即可（钉钉开了加签请填 secret）。企业微信填群机器人 webhook（不是个人微信）。");
      }
    }
    type.addEventListener("change", syncType);
    syncType();
    drawer(editing ? `编辑通知渠道 · ${c.name}` : "新增通知渠道",
      note("按类型只显示需要的字段。"
        + (editing ? " **编辑时配置框留空 = 保持原值不变**。" : "")),
      hint,
      h("div", { class: "grid-2" }, frow(reqLabel("名称"), name), frow(reqLabel("类型"), type)),
      rowUrl, rowSecret, rowSmtp, rowPhone, rowVoice, rowApprise,
      rowAkId, rowAkSec, rowTts, rowTtsKey, rowShowNum, rowVoicePhone,
      rowPlayTimes, rowVolume, rowSpeed, rowOutId,
      h("div", { class: "detail-actions" }, h("button", { class: "btn", onclick: async (e) => {
        // D59 客户端校验：名称必填 ≤64；至少一种投递配置；URL 格式（与服务端 _validate_channel_in 同口径）
        const bad = needText(name.value, "渠道名称", 64);
        if (bad) { toast(bad, "error"); name.focus(); return; }
        const t = type.value;
        const u = url.value.trim(), ap = apprise.value.trim(), sm = smtp.value.trim();
        const ph = phone.value.trim(), vg = voice.value.trim();
        // D152 阿里云语音字段（校验与 payload 收集都要用，故提到外层作用域）
        const akid = akId.value.trim(), aksec = akSec.value.trim();
        const tcode = ttsCode.value.trim(), vph = voicePhone.value.trim();
        const tkey = ttsKey.value.trim();
        if (t === "aliyun_voice") {
          // D152 客户端校验（与服务端 validate_channel 同口径）
          if (!akid) { toast("请填写阿里云 AccessKey ID", "error"); akId.focus(); return; }
          // Secret 出参是掩码：编辑时留空 = 保留旧值，所以只有"新建且没填"才算缺
          if (!aksec && !editing) { toast("请填写阿里云 AccessKey Secret", "error"); akSec.focus(); return; }
          if (!tcode) { toast("请填写语音模板 Code", "error"); ttsCode.focus(); return; }
          if (tkey && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(tkey)) {
            toast("模板变量名只能是字母/数字/下划线，且不以数字开头", "error"); ttsKey.focus(); return;
          }
          if (!vph) { toast("请填写被叫手机号", "error"); voicePhone.focus(); return; }
          // 国内 13800138000 / 国际 +8613800138000 / 座机 0571-12345678 都接受
          if (!/^\+?[0-9][0-9 ()-]{5,20}$/.test(vph)) {
            toast("手机号格式：国内如 13800138000，国际如 +8613800138000", "error"); voicePhone.focus(); return;
          }
          // 外部流水号：官方要求 1~15 字节（阿里云回执会带回它）
          const oid = outId.value.trim();
          if (oid && new TextEncoder().encode(oid).length > 15) {
            toast("外部流水号最多 15 字节（阿里云回执里会带回它）", "error"); outId.focus(); return;
          }
          // 播放参数：留空=用阿里云默认；填了必须在官方允许的区间内
          for (const [label, el, lo, hi] of [["播放次数", playTimes, 1, 3], ["音量", volume, 0, 100], ["语速", speed, -500, 500]]) {
            const raw = el.value.trim();
            if (!raw) continue;
            const num = Number(raw);
            if (!Number.isInteger(num) || num < lo || num > hi) {
              toast(`${label}需为 ${lo}~${hi} 的整数`, "error"); el.focus(); return;
            }
          }
        } else if (t === "phone") {
          if (!ph && !vg) { toast("电话渠道至少要填写手机号或语音网关地址", "error"); phone.focus(); return; }
          if (ph && !/^\+?[0-9][0-9 ()-]{5,20}$/.test(ph)) { toast("手机号格式：E.164，如 +8613800138000", "error"); phone.focus(); return; }
          if (!editing && ph && !vg) { toast("还需要语音网关地址：如 twilio://账号:令牌@主叫号/{phone}", "error"); voice.focus(); return; }
        } else if (!editing || u || ap || sm) {   // 编辑时全留空 = 什么都不改，放行
          if (!(u || ap || sm)) { toast("至少填写一种投递配置：Webhook 地址 / SMTP 串 / apprise_url", "error"); return; }
          const badUrl = needUrl(u, "Webhook 地址");
          if (badUrl) { toast(badUrl, "error"); url.focus(); return; }
          if (sm && sm.split(":").length < 5) { toast("SMTP 串格式：host:port:user:pass:to（5 段冒号分隔）", "error"); return; }
        }
        // 只收集非空字段：编辑时未填的键不进 payload → 服务端保留旧值（掩码不能写回）
        const cfgIn = {};
        if (t === "aliyun_voice") {
          if (akid) cfgIn.access_key_id = akid;
          if (aksec) cfgIn.access_key_secret = aksec;
          if (tcode) cfgIn.tts_code = tcode;
          if (tkey) cfgIn.tts_param_key = tkey;
          if (showNum.value.trim()) cfgIn.called_show_number = showNum.value.trim();
          if (vph) cfgIn.phone = vph;
          // 播放参数：留空就不进 payload → 服务端保留旧值 / 用阿里云默认
          if (playTimes.value.trim()) cfgIn.play_times = Number(playTimes.value.trim());
          if (volume.value.trim()) cfgIn.volume = Number(volume.value.trim());
          if (speed.value.trim()) cfgIn.speed = Number(speed.value.trim());
          if (outId.value.trim()) cfgIn.out_id = outId.value.trim();
        } else if (t === "phone") {
          if (ph) cfgIn.phone = ph;
          if (vg) cfgIn.apprise_url = vg;
        } else {
          if (ap) cfgIn.apprise_url = ap;
          if (u) cfgIn.webhook_url = u;
          if (secret.value.trim()) cfgIn.secret = secret.value.trim();
        }
        if (sm) {
          const [host, port, user, pass, to] = sm.split(":");
          cfgIn.smtp = { host, port: Number(port) || 465, user, pass, to };
        }
        try {
          await busy(e.currentTarget, "保存中…", () => editing
            ? api.put(`/api/ops/notify-channels/${c.id}`, { name: name.value.trim(), type: t, config_json: cfgIn, enabled: c.enabled })
            : api.post("/api/ops/notify-channels", { name: name.value.trim(), type: t, config_json: cfgIn, enabled: true }));
          toast(editing ? "已保存，建议点「测试」验证连通" : "已新增，建议点「测试」验证连通", "ok"); close(); draw();
        } catch (err) { toast(`失败：${err.message}`, "error"); }
      } }, "保存并返回")));
  }

  // ---------- License ----------
  async function drawLicense() {
    let lic = null;
    try { lic = await api.get("/api/license"); } catch (e) { toast(e.message, "error"); }
    const info = lic || {};
    body.replaceChildren(h("div", { class: "card" },
      h("h3", {}, "License 状态"),
      h("div", { class: "detail-meta" },
        kv("状态", badge(info.valid ? "有效" : info.activated_at ? "无效" : "未激活", info.valid ? "PASS" : "FAIL")),
        kv("模块", h("span", { class: "mono" }, (info.modules || []).join(", ") || "-")),
        kv("到期", h("span", { class: "mono" }, info.expires_at || "-")),
        kv("机器指纹", h("span", { class: "mono muted", style: "font-size:0.72rem" }, info.machine_fingerprint || "-")),
        kv("激活时间", fmtTime(info.activated_at))),
      h("div", { class: "note", style: "margin-top:12px" },
        "License 为离线 HMAC 签名串，绑定机器指纹。错误码四类：INVALID_SIGN（签名不符）/ MACHINE_MISMATCH（换机）/ " +
        "EXPIRED（过期）/ CLOCK_ROLLBACK（系统时钟被回拨）。换机需重新签发。"),
      canWrite ? h("div", { class: "form-row" }, h("label", {}, "激活码"),
          h("div", { class: "copy-row" }, h("input", { class: "vs-input mono", id: "lic-key", placeholder: "粘贴 License 串" }),
          h("button", { class: "btn", onclick: async (e) => {
            const key = document.getElementById("lic-key").value.trim();
            if (!key) { toast("请输入激活码", "error"); return; }   // D59：空串别等后端
            try { await busy(e.currentTarget, "激活中…", () => api.post("/api/license/activate", { license_key: key }));
              toast("激活成功", "ok"); draw();
            } catch (err) { toast(`激活失败：${err.message}`, "error"); }
          } }, "激活"))) : null));
  }

  // ---------- 诊断评测（FR-428，admin 专属；数字仅内部可见——红线：不进宣传材料） ----------
  async function drawEval() {
    const [cases, runs] = await Promise.all([
      api.get("/api/ops/eval/cases?page_size=100").catch(() => ({ items: [], total: 0 })),
      api.get("/api/ops/eval/runs").catch(() => ({ items: [], total: 0 })),
    ]);
    const poolSel = h("select", { class: "vs-input" },
      h("option", { value: "regress" }, "回归池（永不参与调优）"),
      h("option", { value: "tune" }, "调优池"));
    const runBtn = h("button", { class: "btn", onclick: async (e) => {
      try {
        const r = await busy(e.currentTarget, "评测运行中…",
          () => api.post("/api/ops/eval/runs", { pool: poolSel.value }));
        toast(`评测完成：${r.total} 个 case，根因一致率 ${(r.match_rate * 100).toFixed(0)}%`, "ok");
        draw();
      } catch (err) { toast(err.message || "评测失败", "error"); }
    } }, "跑一轮评测");

    const caseRows = (cases.items || []).map((c) => [
      String(c.id), c.title, c.expected_label,
      c.origin === "inject" ? "故障注入" : "人工标注",
      c.pool === "regress" ? "回归池" : "调优池",
      c.label_status === "confirmed" ? "已确认" : c.label_status === "pending" ? "待确认" : "已否决",
    ]);
    const runRows = (runs.items || []).map((r) => [
      String(r.id), r.fingerprint.slice(0, 8),
      r.pool === "regress" ? "回归池" : "调优池", String(r.total),
      `${(r.match_rate * 100).toFixed(0)}%`, `${(r.hard_conflict_rate * 100).toFixed(0)}%`,
      `${(r.calibration_rate * 100).toFixed(0)}%`, String(Math.round(r.avg_tokens)),
      (r.created_at || "").slice(0, 19),
    ]);
    const pct = (v, cls) => h("span", { class: "badge " + (cls || "") }, v);

    body.replaceChildren(
      h("div", { class: "card" }, h("h3", {}, "诊断评测（内部质量基线）"),
        h("div", { class: "note" },
          "改提示词 / 技能 / 模型前先跑一轮：同一段录制的故障快照回放重跑（工具响应来自录制、大模型低温重推理），",
          h("br"),
          "对比版本指纹之间的「根因一致率 / 硬冲突率 / 校准率」就知道改好了还是改坏了。评测数字仅内部可见。"),
        h("div", { class: "actions", style: "margin-top:10px" }, poolSel, runBtn)),
      h("div", { class: "card" }, h("h3", {}, `评测 case（${cases.total || 0}）`),
        table(["ID", "标题", "期望根因", "来源", "池", "标注"],
          caseRows, { emptyText: "暂无 case——本地跑 bash deploy/faults/*.sh 注入故障并诊断后固化（见运维手册）" })),
      h("div", { class: "card" }, h("h3", {}, `历次评测（${runs.total || 0}）`),
        table(["ID", "指纹", "池", "case 数", "根因一致", "硬冲突", "校准", "均 token", "时间"],
          runRows, { emptyText: "还没跑过评测" })),
    );
  }

  // ---------- 基础项 ----------
  async function drawBasic() {
    const theme = document.documentElement.getAttribute("data-theme");
    // 改密卡片（D18 补：手册 §1 承诺"首次登录后请立即修改"，但平台原先没有改密入口——
    // 买家部署到公网改不掉默认口令 admin/admin123）
    const oldPwd = h("input", { class: "vs-input", type: "password", placeholder: "当前密码", autocomplete: "current-password" });
    const newPwd = h("input", { class: "vs-input", type: "password", placeholder: "新密码（≥8 位）", autocomplete: "new-password" });
    const newPwd2 = h("input", { class: "vs-input", type: "password", placeholder: "重复新密码", autocomplete: "new-password" });
    const pwdCard = h("div", { class: "card" }, h("h3", {}, "修改密码"),
      h("div", { class: "note" }, "部署到公网前请务必修改默认口令。"),
      h("div", { class: "form-row" }, h("label", {}, "当前密码"), oldPwd),
      h("div", { class: "form-row" }, h("label", {}, "新密码"), newPwd),
      h("div", { class: "form-row" }, h("label", {}, "确认新密码"), newPwd2),
      h("button", { class: "btn", onclick: async (e) => {
        if (!oldPwd.value || !newPwd.value) { toast("请填写完整", "error"); return; }
        if (newPwd.value !== newPwd2.value) { toast("两次输入的新密码不一致", "error"); return; }
        try {
          await busy(e.currentTarget, "提交中…", () => api.post("/api/auth/password", { old_password: oldPwd.value, new_password: newPwd.value }));
          toast("密码已修改，请重新登录", "ok");
          oldPwd.value = newPwd.value = newPwd2.value = "";
        } catch (err) { toast(err.message || "修改失败", "error"); }
      } }, "修改密码"));
    body.replaceChildren(h("div", { class: "grid-2" },
      pwdCard,
      h("div", { class: "card" }, h("h3", {}, "外观"),
        h("div", { class: "form-row" }, h("label", {}, "主题（默认 Light）"),
          select([["light", "Light（默认）"], ["dark", "Dark"]], theme, { onchange: (e) => {
            document.documentElement.setAttribute("data-theme", e.target.value);
            localStorage.setItem("ops_theme", e.target.value);
            toast("主题已切换", "ok");
          } })),
        h("div", { class: "muted", style: "font-size:0.78rem" }, "提供亮色与深色两套主题；图表会随主题重绘。")),
      h("div", { class: "card" }, h("h3", {}, "Agent 接入"), 
        h("div", { class: "note" }, "安装令牌一次性有效（仅用于首次注册新指纹）；已注册主机重启/重装免令牌。"),
        precheckCard(location.origin),
        h("div", { class: "form-row" }, h("label", {}, "一键安装命令"),
          h("div", { class: "copy-row" }, h("input", { class: "vs-input mono", id: "install-cmd", readonly: "readonly", value: "点右侧按钮生成" }),
            h("button", { class: "btn secondary", onclick: async (e) => {
              try { const t = await busy(e.currentTarget, "生成中…", () => api.get("/api/ops/install-token"));
                // 用真实 origin（D15 审计修复，同 hosts.js）：字面占位符粘贴后跑不通
                const cmd = `curl -fsSL ${location.origin}/install.sh | bash -s -- --server ${location.origin} --token ${t.install_token}`;
                document.getElementById("install-cmd").value = cmd;
                // D28：不能直接用 navigator.clipboard（HTTP 公网下 undefined），走降级封装
                copyText(cmd,
                  () => toast("已生成并复制到剪贴板", "ok"),
                  () => toast("已生成（浏览器限制未自动复制，请手动选中复制）", "warn"));
              } catch (e) { toast(`失败：${e.message}`, "error"); }
            } }, "生成"))),
        h("div", { class: "muted", style: "font-size:0.78rem" }, "采集指标 + 服务探活 + 证书到期。执行完成后约 15 秒，本机出现在「主机管理」列表（状态 up）。")),
      h("div", { class: "card" }, h("h3", {}, "平台信息"),
        h("div", { class: "detail-meta" },
          kv("当前用户", `${me.username} · ${me.role}`),
          kv("数据保留", h("span", { class: "muted" }, "指标明细 2h（内存环形）/ 聚合 7d / 事件 180d")),
          kv("案例库", h("span", { class: "mono", id: "case-stat" }, "…")),
          kv("向量召回", h("span", { class: "mono", id: "vec-stat" }, "…")))),
      ));
    try { const s = await api.get("/api/ops/case-cards/stats");
      const a = document.getElementById("case-stat"), b = document.getElementById("vec-stat");
      if (a) a.textContent = `${s.total} 张`;
      if (b) b.textContent = s.vector_enabled ? "已启用（l2_distance）" : "已降级为文本匹配";
    } catch { /* ignore */ }
  }

  function frow(label, el) { return h("div", { class: "form-row" }, h("label", {}, label), el); }
  function kv(k, v) { return h("div", { class: "kv" }, h("span", { class: "kv-k muted" }, k), h("span", { class: "kv-v" }, v)); }
  const close = () => document.querySelector(".detail-head .btn")?.click();
  await draw();
}

export function destroy() {}
