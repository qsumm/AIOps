// changelog.js —— 更新公告内容数据源（FR-421）
//
// 为什么是独立静态数据文件，而不是接口或 i18n 字典：
//   ① 免构建、零新端点（不触发 ROUTES 登记与接口文档重生成），随前端一起发布；
//   ② 它是**内容**（发版说明），不是 **UI chrome**（按钮文案、标签）——i18n 字典只管后者，
//      把几十条双语发版说明塞进字典会让字典不可维护；
//   ③ 与平台其它"内容即数据"的先例一致（如内置手册、案例库）。
//
// 字段约定：
//   id     —— 条目唯一标识（已读状态用它）；**新增条目必须用新 id**，不可复用旧 id，
//              否则老用户会被判成"已读"而永远看不到这次更新。
//   date   —— 发布/上线日期（ISO），只作展示，不参与版本比较。
//   level  —— major（功能级更新，自动弹分栏大卡）/ minor（优化修订，右上角轻量卡片）/ patch（内部修复，不打扰）。
//   label  —— 角标文案（ZH/EN）。
//   title  —— 一句话主标题（ZH/EN）——写**用户能多做什么**，不写实现细节。
//   summary—— 一句话导语（ZH/EN）。
//   items  —— 2~5 条变更；每条 { lead（加粗小标题）, text（说明）}，lead+text 双语。
//   link   —— 「查看详情」跳转目标（页面内 hash 路由，如 "#/manual/3"；无则省略该键）。
//
// 维护纪律：
//   - 发版时**同时**更新：仓库根 VERSION、《版本发布说明》最新标题、手册「修订记录」最新行、
//     以及本文件的**顶部条目**（守卫测试 test_d130_whatsnew.py 机械核对前四项）。
//   - 本文件顶部的 version 字段是**声明值**（该批功能对应的产品版本）；弹窗界面上显示的
//     版本号一律取运行时 /api/health 的 version（唯一真源），不在此处自造。
//   - 双语必须对称：任一语言缺字段，守卫测试会红。

export const RELEASES = [
  {
    id: "2026-10-24-no-overwrite",
    version: "1.8.1",
    date: "2026-10-24",
    level: "patch",
    label: { zh: "修复", en: "Fix" },
    title: { zh: "诊断失败不再覆盖已有结论", en: "Failed runs no longer overwrite findings" },
    summary: {
      zh: "LLM 暂不可用时的失败报告不再把事件上已有的成功结论覆盖成“证据不足”，也不再误发“需人工”通知；评测回放改为纯内存执行。",
      en: "Failed diagnosis runs no longer overwrite existing good reports with INCONCLUSIVE stubs or send false needs-human notices; eval replay now runs fully in memory.",
    },
    items: [
      { lead: { zh: "结论不被误覆盖", en: "No overwrite" },
        text: { zh: "只有事件还没有任何报告时，失败才会占位；已有成功结论永远保留。",
                en: "A failed run only occupies the report slot when none exists; good conclusions are kept." } },
    ],
  },
  {
    id: "2026-10-24-eval-loop",
    version: "1.8.0",
    date: "2026-10-24",
    level: "minor",
    label: { zh: "新增能力", en: "New" },
    title: { zh: "AI 诊断有了自己的考试系统", en: "An exam system for AI diagnosis" },
    summary: {
      zh: "把真实故障诊断固化为可回放的评测 case；改提示词/技能/模型前后各跑一轮，一致率、硬冲突率、校准率一目了然——防止越改越笨。",
      en: "Record real diagnoses as replayable eval cases; run before/after any prompt/skill/model change and compare match rate, hard conflicts and calibration.",
    },
    items: [
      { lead: { zh: "录制-回放", en: "Record & replay" },
        text: { zh: "评测时工具响应来自录制快照（不碰真实主机），大模型低温重推理——同一段上下文确定性复现。",
                en: "Tool responses come from recorded snapshots while the LLM re-reasons at temperature 0 — deterministic replay." } },
      { lead: { zh: "双池防过拟合", en: "Two pools" },
        text: { zh: "调优池与回归池硬隔离：回归池的 case 永不参与调优，只做改前改后对比。",
                en: "Tune and regress pools are strictly separated; regress cases never feed tuning." } },
    ],
  },
  {
    id: "2026-10-24-login-audit",
    version: "1.7.4",
    date: "2026-10-24",
    level: "patch",
    label: { zh: "审计修复", en: "Audit" },
    title: { zh: "操作审计补上登录记录，时间口径标注清楚", en: "Login auditing and timezone clarity" },
    summary: {
      zh: "登录（含失败尝试）真正进入操作审计；审计时间列明确标注北京时间，日期筛选不再错位 8 小时。",
      en: "Logins (including failures) are now audited; audit times are labeled Beijing time and the date filter no longer shifts by 8 hours.",
    },
    items: [
      { lead: { zh: "登录有痕", en: "Logins on record" },
        text: { zh: "成功登录与失败尝试都进操作审计（含来源 IP 与尝试的用户名）——爆破尝试可查。",
                en: "Successful and failed logins are audited with source IP and attempted username." } },
      { lead: { zh: "时间不再误读", en: "Clear timestamps" },
        text: { zh: "审计时间列标注「北京时间」，日期筛选同步按北京时间切日。",
                en: "Audit times are labeled Beijing time; the date filter cuts by Beijing day." } },
    ],
  },
  {
    id: "2026-10-24-manual-audit",
    version: "1.7.3",
    date: "2026-10-24",
    level: "patch",
    label: { zh: "文档与指南", en: "Docs" },
    title: { zh: "手册全量校对：新功能描述补齐，33 张截图更新到当前界面", en: "Manual fully audited and re-illustrated" },
    summary: {
      zh: "补齐 AI 判断列、问题特征、token「用在哪儿」与 MCP 端点的使用说明；手册 33 张截图全部更新到当前界面。",
      en: "Added usage docs for the AI column, incident highlights, token breakdown and the MCP endpoint; all 33 manual screenshots refreshed.",
    },
    items: [
      { lead: { zh: "新功能说明补齐", en: "New feature docs" },
        text: { zh: "「AI 判断」列、问题特征卡、token「用在哪儿」分账与 MCP 端点（默认关闭）的用法，现在手册里都有；系统设置子节编号修正。",
                en: "The manual now covers the AI judgment column, incident highlight cards, token breakdown by purpose, and the (off-by-default) MCP endpoint." } },
      { lead: { zh: "截图与界面一致", en: "Screenshots match" },
        text: { zh: "技能页新字段、下线/移出按钮、大盘健康率行为等配图全部重截；手册 PDF/Word 已重新生成。",
                en: "All manual screenshots re-captured against the current UI; the PDF/Word manual has been regenerated." } },
    ],
  },
  {
    id: "2026-10-24-dashboard-accuracy",
    version: "1.7.2",
    date: "2026-10-24",
    level: "patch",
    label: { zh: "统计修复", en: "Fix" },
    title: { zh: "大盘健康率不再统计已移出的主机", en: "Dashboard health rate excludes removed hosts" },
    summary: {
      zh: "「移出」的主机此前仍计入大盘健康率分母并在宕机名单里显示标识哈希；现在已移出主机退出大盘全部聚合。",
      en: "Removed hosts no longer count into the dashboard health rate or appear as raw hashes in the down list.",
    },
    items: [
      { lead: { zh: "移出 = 从视野消失", en: "Removed means gone" },
        text: { zh: "移出主机不再影响健康率、宕机名单、磁盘榜与资源 Top5；「下线」主机仍如实计入（下线≠健康）。",
                en: "Removed hosts no longer affect the health rate, down list, disk board or resource Top5; manually offline hosts still count (offline is not healthy)." } },
    ],
  },
  {
    id: "2026-10-23-comprehension",
    version: "1.7.1",
    date: "2026-10-23",
    level: "patch",
    label: { zh: "体验修订", en: "Polish" },
    title: { zh: "看得懂：报告不再误报失败，列表直接说清是什么问题", en: "Clearer reports and lists" },
    summary: {
      zh: "修复「已有结论却提示生成失败」的误判；提示弹窗至少停留 6 秒；问题看板与告警中心直接显示问题特征（指标中文名+读数、AI 结论一句话、AI 判断列）。",
      en: "Fixed the false diagnosis-failed notice; toasts last at least 6s; boards now show what the problem actually is (Chinese metric names, readings, one-line AI conclusions).",
    },
    items: [
      { lead: { zh: "误报修复", en: "False alarm fixed" },
        text: { zh: "诊断明明有完整结论，页面却提示「生成失败」——已修复；现在按报告内容判断是否有可分诊的结论。",
                en: "A full report no longer shows \"diagnosis failed\"; triage availability is judged from report content." } },
      { lead: { zh: "问题一眼可读", en: "Problems at a glance" },
        text: { zh: "问题看板卡片与告警列表显示中文指标名和触发读数，已有结论的事件直接带一句话摘要；告警列表新增「AI 判断」列。",
                en: "Cards and alert rows show Chinese metric names with readings; incidents with conclusions show a one-line summary; alerts gain an AI column." } },
    ],
  },
  {
    id: "2026-10-23-skill-upgrade",
    version: "1.7.0",
    date: "2026-10-23",
    level: "major",
    label: { zh: "新功能上线", en: "New feature" },
    title: {
      zh: "技能体系升级：自动触发、可授权命令、可查效果",
      en: "Skills upgraded: auto-trigger, authorized commands, measurable effect",
    },
    summary: {
      zh: "排查方法不再只靠 AI 自觉想起来：告警一旦命中适用范围的技能，技能内容会自动进入本次诊断；技能还能带上少量只读命令；技能页新增「效果」列，用真实诊断流水说话。",
      en: "Runbooks no longer rely on the AI remembering to load them: matching skills are injected automatically, skills may carry a few read-only commands, and a new Effect column reports what real diagnostics show.",
    },
    items: [
      { lead: { zh: "按告警自动加载", en: "Auto-trigger" },
        text: { zh: "给技能填「适用告警模式」（如 disk.*、*内存*），命中该类告警的诊断会直接带上技能全文，不用等 AI 想起来。",
                en: "Fill in a skill's alert scope; diagnoses on matching alerts load it automatically." } },
      { lead: { zh: "技能授权只读命令", en: "Authorized commands" },
        text: { zh: "技能可声明少量只读命令（如 mysql 查进程列表），仅在加载了该技能的这次诊断内临时放行；平台与目标机双侧校验，危险命令黑名单永远优先。",
                en: "A skill may declare a few read-only commands, valid only inside that diagnosis; both sides validate and dangerous commands are always refused." } },
      { lead: { zh: "效果看得见", en: "Measurable effect" },
        text: { zh: "技能页「效果」列显示加载次数、跟进率与 Token 中位数，全部可从真实诊断流水复算；样本不足如实写「样本不足」。",
                en: "The Effect column shows load count, follow-up rate and median tokens, all recomputable from real records; small samples are reported as insufficient." } },
      { lead: { zh: "从 GitHub 导入", en: "Import from GitHub" },
        text: { zh: "粘贴 GitHub 技能链接即可导入（只允许 github.com 域名，≤1MB）；重名会被拒绝，不会覆盖已有技能。",
                en: "Paste a GitHub skill link to import it (github.com only, ≤1MB); duplicate names are refused." } },
    ],
  },
  {
    id: "2026-10-22-triage",
    version: "1.6.0",
    date: "2026-10-22",
    level: "minor",
    label: { zh: "新功能上线", en: "New feature" },
    title: {
      zh: "AI 诊断报告可以人工分诊了",
      en: "Triage AI diagnosis reports",
    },
    summary: {
      zh: "每份诊断报告可标记跟进/观察/忽略（忽略必填理由）；同主机的忽略历史在后续报告自动提示——你的判断会沉淀为经验。",
      en: "Mark each report follow-up / observing / dismissed (reason required); dismissals are remembered per host.",
    },
    items: [
      { lead: { zh: "分级初筛", en: "Triage tiers" },
        text: { zh: "报告按自评强度标注「建议必跟/建议跟进/可忽略区」；AI 只初筛，最终以人工处置为准。", en: "Reports are pre-sorted by self-assessed strength; humans make the final call." } },
      { lead: { zh: "降噪复用", en: "Noise reduction" },
        text: { zh: "忽略的理由会留痕，同主机后续报告自动引用——重复噪音不再以同样面目出现。", en: "Dismissal reasons are reused on later reports for the same host." } },
    ],
  },
  {
    id: "2026-10-22-recovery-toggle",
    version: "1.5.6",
    date: "2026-10-22",
    level: "patch",
    label: { zh: "内部修复", en: "Patch" },
    title: { zh: "「恢复通知」开关修复", en: "Recovery-notify toggle fixed" },
    summary: {
      zh: "规则的「恢复通知」设为关闭后，恢复消息此前仍会发出；现全路径生效。",
      en: "Turning off recovery notifications now actually silences them on every delivery path.",
    },
    items: [
      { lead: { zh: "恢复通知开关生效", en: "Toggle now works" },
        text: { zh: "分级通知与通知路由两条路径都尊重该开关；关闭后一条不发，通知台账里留有说明。", en: "Both tiered and routed paths honor the switch; nothing is sent and the log explains why." } },
    ],
  },
  {
    id: "2026-10-22-delivery",
    version: "1.5.5",
    date: "2026-10-22",
    level: "minor",
    label: { zh: "交付修复 + 新功能", en: "Fixes & features" },
    title: {
      zh: "告警有了历史页签，电话可以打给一整组人",
      en: "Alert history tab and multi-number voice calls",
    },
    summary: {
      zh: "修复交付前测试发现的全部问题，并新增两项常用能力：",
      en: "All issues found in pre-delivery testing are fixed, plus two frequently requested capabilities:",
    },
    items: [
      { lead: { zh: "告警历史页签", en: "History tab" },
        text: { zh: "恢复/关闭的告警全部归档可查：按级别/主机/关键词过滤，详情含触发与恢复时间。", en: "Recovered and closed alerts stay searchable by level, host and keyword, with trigger and recovery times." } },
      { lead: { zh: "语音多被叫号码", en: "Multi-number voice" },
        text: { zh: "被叫手机号支持逗号分隔多个，告警逐个拨打——值班组一个渠道全覆盖。", en: "Comma-separate numbers to call the whole on-call group from one channel." } },
      { lead: { zh: "五项修复", en: "Five fixes" },
        text: { zh: "证书探活接受 https:// 写法；读 /etc/passwd 不再误拦；HTTP 探活检查命令双侧对齐；非法请求体明确报错；内部签发入口不再暴露。", en: "Cert probes accept https://; reading /etc/passwd no longer blocked; curl checks aligned; bad bodies get clear 422s; internal endpoint hidden." } },
    ],
  },
  {
    id: "2026-10-22-voice-text",
    version: "1.5.4",
    date: "2026-10-22",
    level: "patch",
    label: { zh: "内部修复", en: "Patch" },
    title: { zh: "告警语音播报内容优化", en: "Voice announcement content improved" },
    summary: {
      zh: "电话播报改为中文指标与具体对象：磁盘/内存使用率、阈值与主机名，并以「请尽快处理」收尾。",
      en: "Announcements now read localized metric names with dimensions, thresholds and host, ending with a call to action.",
    },
    items: [
      { lead: { zh: "中文播报 + 哪块盘", en: "Chinese + which disk" },
        text: { zh: "指标名转中文并带维度（挂载点/网卡/实例）；触发结尾自动加「请尽快处理」。", en: "Metric names localized with dimensions (mount, NIC, instance); recovery-act hint appended." } },
    ],
  },
  {
    id: "2026-10-22-node-map",
    version: "1.5.3",
    date: "2026-10-22",
    level: "minor",
    label: { zh: "能力补齐", en: "Capability" },
    title: {
      zh: "node_exporter 直接接入，案例库语义检索修复",
      en: "Direct node_exporter ingestion; case semantic search fixed",
    },
    summary: {
      zh: "装了 node_exporter 的机器零改造进平台；案例库语义检索首次真实生效。",
      en: "node_exporter hosts plug in with zero changes; semantic case search now really works.",
    },
    items: [
      { lead: { zh: "190 项映射", en: "190 mappings" },
        text: { zh: "node_* 指标转平台指标名并保留维度（每块盘/每张网卡/每条 TCP 状态），规则可按设备下钻。", en: "node_* metrics map with dimensions kept — per disk, per NIC, per TCP state." } },
      { lead: { zh: "降级通道", en: "Fallback channel" },
        text: { zh: "模型配置可勾「作为降级通道」：主模型额度耗尽时自动切换（平台不替你选模型）。", en: "Mark a model config as fallback; when the primary runs dry the platform switches — never choosing for you." } },
    ],
  },
  {
    id: "2026-10-21-trio",
    version: "1.5.0",
    date: "2026-10-21",
    level: "major",
    label: { zh: "新功能上线", en: "New release" },
    title: {
      zh: "通知路由、外部指标源、多维指标查询一次到位",
      en: "Notify routes, external metrics and drill-down in one release",
    },
    summary: {
      zh: "本版同时完善告警通知分发与指标接入两条主线：",
      en: "This release improves both alert routing and metrics ingestion:",
    },
    items: [
      { lead: { zh: "通知路由", en: "Notify routes" },
        text: { zh: "按级别/标签/时段把告警路由给不同团队，命中即接管；带试投验证。", en: "Route alerts to teams by level, tags and time windows, with dry-run validation." } },
      { lead: { zh: "外部指标源", en: "External metrics" },
        text: { zh: "直采 exporter 的 /metrics，多实例维度自动保留，业务指标白名单原样入库。", en: "Scrape exporters directly, keep per-instance dimensions, whitelist business metrics verbatim." } },
      { lead: { zh: "指标查询下钻", en: "Metric drill-down" },
        text: { zh: "按指标+维度选择器即时查询，超窗自动从落库历史回补。", en: "Instant queries with dimension selectors; older windows backfill from storage." } },
      { lead: { zh: "MCP 端点", en: "MCP endpoint" },
        text: { zh: "外部 AI 客户端可经平台自带工具集查询（默认关闭）。", en: "External AI clients can query via the platform toolset (off by default)." } },
    ],
  },
  {
    id: "2026-10-20-aliyun-voice",
    version: "1.4.0",
    date: "2026-10-20",
    level: "major",
    label: { zh: "新功能上线", en: "New release" },
    title: {
      zh: "S1 紧急告警可以直接打电话了",
      en: "S1 alerts can now call you",
    },
    summary: {
      zh: "新增「阿里云语音」通知渠道：用你的 AccessKey 直连阿里云语音服务，把告警内容念给值班人。",
      en: "New Aliyun Voice channel: platform signs RPC calls to Aliyun Voice Service and reads alerts aloud.",
    },
    items: [
      { lead: { zh: "真拨号播报告警", en: "Real calls, real content" },
        text: { zh: "填 AccessKey 与语音模板 Code 即用；告警内容塞进模板变量播报，超 200 字自动截断。", en: "Fill in AccessKey and TTS template code; alert content is injected into the template variable." } },
      { lead: { zh: "计入电话类渠道", en: "Counts as phone" },
        text: { zh: "S1 紧急告警的分级通知可以只选它，规则保存校验与派发兜底都认。", en: "Counts as a phone channel for S1 tiered notifications, with validation and fallback." } },
      { lead: { zh: "错误码可检索", en: "Searchable errors" },
        text: { zh: "测试失败给出阿里云错误码 + 中文解释 + 下一步（测试按钮会真拨一通，先弹确认）。", en: "Failures show Aliyun error code with plain-language cause and next step (test really calls)." } },
    ],
  },
  {
    id: "2026-10-19-monitor",
    version: "1.3.0",
    date: "2026-10-19",
    level: "major",
    label: { zh: "新功能上线", en: "New release" },
    title: {
      zh: "平台学会了自己查健康",
      en: "The platform now diagnoses itself",
    },
    summary: {
      zh: "这版把「发现 → 诊断 → 处置」接上了，先看影响最大的三件事：",
      en: "This release connects discovery → diagnosis → remediation. Here are the three that matter most:",
    },
    // highlights —— 弹窗「更新要点」用的短句版（带右侧数据锚点）。
    // 与 items 的分工：items 是完整说明（更新中心 / 轻量卡片用），highlights 是给弹窗一屏读完的摘要。
    highlights: [
      {
        title: { zh: "健康诊断", en: "Health diagnosis" },
        desc: { zh: "10 项检查自动出报告，大模型限流时照样可用", en: "10 read-only checks produce a report automatically — works even when the LLM quota is out" },
        stat: { zh: "2s", en: "2s" },
        statLabel: { zh: "出报告", en: "to report" },
      },
      {
        title: { zh: "主机详情六页签", en: "Six-tab host detail" },
        desc: { zh: "进程、网卡、每分区磁盘、TCP 状态逐项可查", en: "Processes, NICs, per-partition disks and TCP states, item by item" },
        stat: { zh: "6", en: "6" },
        statLabel: { zh: "个页签", en: "tabs" },
      },
      {
        title: { zh: "Agent 平台一键升级", en: "One-click Agent upgrade" },
        desc: { zh: "点一下自动完成，无需登机敲命令", en: "One click and it is done — no more SSH commands" },
        stat: { zh: "≤15s", en: "≤15s" },
        statLabel: { zh: "生效", en: "to apply" },
      },
    ],
    items: [
      {
        lead: { zh: "采集更细", en: "Finer collection" },
        text: {
          zh: "CPU/内存分解、负载、磁盘读写与 IOPS、inode、TCP 连接按状态、网卡错误丢包；进程快照扩为「CPU ∪ 内存 TopN」，内存泄漏型故障不再漏检。",
          en: "CPU/memory breakdown, load, disk IO and IOPS, inode, TCP states, NIC errors and drops; process snapshots now cover both CPU and memory leaders.",
        },
      },
      {
        lead: { zh: "六页签监控面板", en: "Six-tab monitor" },
        text: {
          zh: "主机详情拆成概览 / 基础监控 / 操作系统 / 网络 / 进程 / 健康诊断，3×3 图表网格，时间范围 1h~30d，图表上直接叠加告警标记线——故障从哪一刻开始，一眼看到。",
          en: "Host details split into six tabs with a 3×3 chart grid, 1h–30d ranges, and alert markers overlaid on charts to show exactly when a fault began.",
        },
      },
      {
        lead: { zh: "健康诊断不依赖大模型", en: "Diagnosis without an LLM" },
        text: {
          zh: "内置 10 项只读规则检查（CPU / 内存与 OOM / 负载匹配 / 磁盘水位 / IO 饱和 / inode / TCP / 丢包 / 探活 / 服务可用性），约 2 秒出报告；大模型额度耗尽时诊断依然可用。",
          en: "Ten built-in read-only checks (CPU, memory and OOM, load mismatch, disk, IO saturation, inode, TCP, packet loss, probes, service availability) report in about two seconds, even when the LLM quota is exhausted.",
        },
      },
      {
        lead: { zh: "Agent 一键自升级", en: "One-click Agent upgrade" },
        text: {
          zh: "在平台上直接升级被监控机上的 Agent：下载 → SHA256 校验 → 原子替换 → 自动重启，版本漂移由文件指纹判定，不会出现「同版本号但内容变了推不下去」。",
          en: "Upgrade the Agent on monitored hosts from the platform: download, SHA256 verification, atomic replace, and auto-restart, with fingerprint-based drift detection.",
        },
      },
      {
        lead: { zh: "顺手补的顺手活", en: "Smaller wins" },
        text: {
          zh: "AI 助手可聚焦最多 5 台主机提问；告警中心支持主机/关键词筛选与批量删除；主机列表标注「在线但没在采集」，安装失败有明确的三步排查指引。",
          en: "The AI assistant can focus on up to five hosts; the alert center gains host/keyword filters and bulk delete; hosts flag 'up but not reporting', and install failures get a clear three-step checklist.",
        },
      },
    ],
    link: "#/manual/3",
  },
  {
    id: "2026-10-18-agent-health",
    version: "1.3.0",
    date: "2026-10-18",
    level: "major",
    label: { zh: "能力增强", en: "Capability upgrade" },
    title: {
      zh: "Agent 更可靠了：凭证自愈、僵尸告警不再干扰诊断、日志路径自动发现",
      en: "A more reliable Agent: credential self-healing, stale alerts no longer skew diagnosis, automatic log path discovery",
    },
    summary: {
      zh: "把「不知道是没开机还是凭证失效」「诊断被旧告警带偏」「日志到底在哪」这三类模糊，变成界面上能直接看见的结论。",
      en: "Ambiguity about offline hosts, skewed diagnosis, and unknown log locations now resolves into visible conclusions.",
    },
    items: [
      {
        lead: { zh: "Agent 认证自愈", en: "Credential self-healing" },
        text: {
          zh: "凭证失效自动重新注册；主机列表可看认证状态、最近成功上报、缓存积压与最近错误——「离线」从此可信。",
          en: "Expired credentials re-register automatically, and host pages show auth state, last successful report, cache backlog, and recent errors.",
        },
      },
      {
        lead: { zh: "告警新鲜度", en: "Alert freshness" },
        text: {
          zh: "规则停用或久未评估的活跃告警标记为「数据陈旧」，不再作为当前故障证据进入通知、事件收敛与 AI 诊断。",
          en: "Alerts from disabled or long-unevaluated rules are marked stale and excluded from notifications, clustering, and AI diagnosis.",
        },
      },
      {
        lead: { zh: "日志路径自动发现", en: "Automatic log discovery" },
        text: {
          zh: "取证不再要求事先声明日志路径：按服务定位进程，从其打开的文件中发现日志，发现结果可一键采纳为「建议日志路径」。",
          en: "Log collection no longer needs a declared path: the Agent locates the process, discovers its open files, and the result can be adopted as a suggested log path.",
        },
      },
      {
        lead: { zh: "报告可回放", en: "Replayable reports" },
        text: {
          zh: "报告里的每个 step_N 引用可点击跳转工具瀑布并高亮，直接看到那一步的完整参数与输出。",
          en: "Every step_N reference in a report links into the tool waterfall and highlights the exact call parameters and output.",
        },
      },
    ],
    link: "#/manual/3",
  },
  {
    id: "2026-10-16-service-log",
    version: "1.2.0",
    date: "2026-10-16",
    level: "major",
    label: { zh: "新功能上线", en: "New release" },
    title: {
      zh: "服务级监控 + 日志取证 + 分级恢复：出事不用再 SSH 上机器翻日志",
      en: "Service monitoring, log forensics, and tiered recovery: no more SSHing in to read logs",
    },
    summary: {
      zh: "把「连接打满」「进程假死」「磁盘打满」这三类最常见的线上故障，纳入平台可观测、可取证、可恢复的闭环。",
      en: "The three most common production faults—connection exhaustion, hung processes, and full disks—become observable, provable, and recoverable inside the platform.",
    },
    items: [
      {
        lead: { zh: "服务对象", en: "Service objects" },
        text: {
          zh: "MySQL / Redis / Nginx / MQ 按连接池、队列、内存等关键指标判健康，服务异常会主动告警。",
          en: "MySQL, Redis, Nginx, and MQ are judged by pool, queue, and memory metrics, and unhealthy services raise alerts on their own.",
        },
      },
      {
        lead: { zh: "日志取证", en: "Log forensics" },
        text: {
          zh: "以告警首次触发时刻为锚，取回相关日志片段并附覆盖报告；诊断报告的证据段直接引用日志原文。",
          en: "Log excerpts are retrieved around the first trigger with a coverage report, and diagnosis reports quote the original log text.",
        },
      },
      {
        lead: { zh: "分级恢复 + 九道护栏", en: "Tiered recovery with nine guardrails" },
        text: {
          zh: "建议、人工确认、白名单自动三档执行；第 2 次失败即自动停止转人工，不允许无限重试。",
          en: "Suggest, manual-confirm, and allowlisted-auto modes, each behind nine guardrails; a second failure stops automation and hands over to a human.",
        },
      },
    ],
    link: "#/manual/10",
  },
  {
    id: "2026-10-16-ui-docs",
    version: "1.2.1",
    date: "2026-10-16",
    level: "minor",
    label: { zh: "体验改进", en: "Improvements" },
    title: {
      zh: "界面中英双语、文档中心改版、平台演示视频",
      en: "Bilingual UI, redesigned docs center, and a platform demo video",
    },
    summary: {
      zh: "语言跟随账号记忆，文档支持目录跳转与搜索，演示视频按界面语言自动选片。",
      en: "Language follows the account, docs support jump-to and search, and the demo video picks its language automatically.",
    },
    items: [
      {
        lead: { zh: "中英切换", en: "Language switch" },
        text: {
          zh: "右上角中/EN 切换，语言偏好按账号保存，登录页与导航全英文，缺词自动回退中文。",
          en: "A ZH/EN switch in the top bar, remembered per account, with a full-English shell and graceful fallback.",
        },
      },
      {
        lead: { zh: "文档中心三栏", en: "Three-column docs" },
        text: {
          zh: "右栏本页目录可点击跳转并滚动高亮，正文加宽，框架固定只滚内容。",
          en: "A clickable right-hand table of contents with scroll highlighting, a wider body, and only the content scrolling.",
        },
      },
      {
        lead: { zh: "AI 助手上下文管理", en: "Assistant context" },
        text: {
          zh: "滚动摘要 + 最近窗口、上下文用量指示、手动「压缩历史」按钮；并新增五类只读历史查询工具。",
          en: "Rolling summaries, a recent window, usage indicators, a manual compaction action, and five new read-only history tools.",
        },
      },
    ],
    link: "#/manual",
  },
  {
    id: "2026-10-15-docs-center",
    version: "1.1.14",
    date: "2026-10-15",
    level: "minor",
    label: { zh: "体验改进", en: "Improvements" },
    title: {
      zh: "内置文档中心与版本信息页：不用再翻邮件里的附件",
      en: "Built-in docs center and version page: no more digging through email attachments",
    },
    summary: {
      zh: "使用手册、快速开始随平台分发，可搜索、支持深链与 PDF 下载；版本号改为单一来源。",
      en: "The manual and quick start ship with the platform, searchable and deep-linkable, with a downloadable PDF; the version number now has a single source.",
    },
    items: [
      {
        lead: { zh: "文档内置", en: "Docs built in" },
        text: {
          zh: "手册 / 快速开始随镜像分发，离线可看可搜，支持 #/manual/6 这样的章节深链。",
          en: "The manual and quick start ship inside the image, readable and searchable offline, with chapter deep links like #/manual/6.",
        },
      },
      {
        lead: { zh: "版本号单一来源", en: "Single version source" },
        text: {
          zh: "版本号只在 VERSION 文件里定义一次，界面、接口、手册、发布说明全部由它派生。",
          en: "The version is defined once in the VERSION file and derived everywhere: UI, API, manual, and release notes.",
        },
      },
    ],
    link: "#/about",
  },
];

/** 条目 id → 条目（找不到返回 null）。 */
export function releaseById(id) {
  return RELEASES.find((r) => r.id === id) || null;
}

/** 当前语言下的展示文案（缺词回退中文，再回退空串——守卫测试会兜住缺词）。 */
export function localized(entry, field) {
  const v = entry && entry[field];
  if (!v) return "";
  if (typeof v === "string") return v;
  const lang = (typeof window !== "undefined" && window.__USER_LANG__) || "zh";
  return v[lang] || v.zh || "";
}
