/**
 * AI operability contract, step three: every user-operable control is either commandable through
 * `UiChanges`, reachable through a catalog capability, explicitly exempt (protected core, free-text
 * input) — or a known gap that this list names until it is closed. The e2e audit walks the surfaces,
 * collects the a11y controls and classifies each one against these tables; a control that fits none
 * fails the run, and a known gap that disappeared is reported so the list can be trimmed.
 *
 * Keep this in step with docs/FUTURES_AGENT_CAPABILITY_V1.md (the authoritative coverage table).
 */
export interface Control {
  surface: string;
  role: string;
  label: string;
  disabled: boolean;
  observed: boolean;
  /** Settings category (section aria-label) or nearest `data-agent-target`; empty when neither. */
  group?: string;
}
type Matcher = RegExp | ((control: Control) => boolean);
interface Rule { match: Matcher; via: string }
interface Exemption { match: Matcher; reason: string }
export interface KnownGap { match: Matcher; gap: string; suggestion: string }

const test = (matcher: Matcher, control: Control) => matcher instanceof RegExp ? matcher.test(control.label) : matcher(control);
/** 提醒 (2026-10-15; a right-rail panel since 2026-10-25): the computer's 提醒 panel, the phone's 提醒 page and AI 首页's 最新提醒 block. */
const REMINDER_GROUPS = new Set(["panel:reminders", "mobile-reminders", "mobile-latest-reminders"]);

/** Controls a UI command reproduces (the `via` names the command or the capability). */
export const COMMANDABLE: Rule[] = [
  // Local remote controls use the device store; hardware selection remains a user gesture.
  { match: control => control.surface === "settings-voice" && control.role === "switch" && control.label === "启用米家遥控器", via: "remoteSettings: {enabled: true|false}" },
  { match: control => control.surface === "settings-voice" && control.role === "button" && control.label === "断开遥控器", via: "remoteSettings: {disconnect: true}" },
  { match: control => control.surface === "settings-voice" && control.role === "button" && control.label === "打开按键设置", via: "settingsPage: remote-keys" },
  { match: control => control.surface === "settings-voice" && control.role === "button" && control.label === "打开测试与诊断", via: "settingsPage: remote-diagnostics" },
  { match: control => control.surface === "settings-remote-keys" && control.role === "select" && /^(上|下|左|右|确认|返回|主页|菜单|TV|音量＋|音量－|语音|电源)动作$/.test(control.label), via: "remoteSettings: {bindings: {按键 id: 动作}}（仅 voice 允许 ptt）" },
  { match: control => control.surface === "settings-remote-keys" && control.role === "button" && control.label === "恢复默认按键", via: "remoteSettings: {resetBindings: true}（快照 bindingsDefault）" },
  { match: control => control.surface === "settings-remote-diagnostics" && control.role === "button" && /^(开始测试|停止测试)$/.test(control.label), via: "remoteSettings: {diagnostic: true|false}（开始须连接 ready；离页停止）" },
  { match: /^快捷手数 [125]$/, via: "quickVolume: 1|2|5（快照 state.quickVolume；仅改本机手数）" },
  { match: /^设置快捷键$/, via: "settingsPage: shortcuts" },
  { match: /^(展开五档|收起五档)$/, via: "disclosure: {name: order-depth, open: true|false}" },
  { match: control => control.role === "tab" && control.surface !== "dialog-help", via: "symbol" },
  // 策略编辑卡 (2026-10-08; before the generic rules — its 1分 / 上线运行… / 更多： mean card things).
  { match: control => control.group === "strategy-editor" && control.label === "放弃", via: "strategyEditor: {close: true}（放弃没存的改动并关卡）" },
  { match: control => control.group === "strategy-editor" && /^(空白|按我的习惯|全部清空)$/.test(control.label), via: "strategyEditor: {start: blank | habits}（起点；全部清空 = blank）" },
  { match: control => control.group === "strategy-editor" && control.label === "从模板", via: "strategyEditor: {start: template:<模板id>}（模板 id 见 GET /api/strategy-templates）" },
  { match: control => control.group === "strategy-editor" && /^查看这 \d+ 笔$/.test(control.label), via: "disclosure: strategy-editor-trades（预填依据的成交；快照 state.strategyEditor.prefill）" },
  { match: control => control.group === "strategy-editor" && /^(改|AI 在改…)$/.test(control.label), via: "strategyEditor: {ask: 一句话}（= POST /api/strategy-plans/design-jobs，改到的字段高亮、不保存）" },
  { match: control => control.group === "strategy-editor" && (/^(上一项|下一项|回答并完善策略)$/.test(control.label) || control.role === "radio"),
    via: "strategyEditor: {answers: {问题 id: 回答}}（答完带着答案重新生成；快照 state.strategyEditor.questions）" },
  { match: control => control.group === "strategy-editor" && control.role === "summary" && /^更多：/.test(control.label), via: "disclosure: strategy-editor-more（保本、分批止盈、按时间止盈、最长持仓、按条件平仓）" },
  { match: control => control.group === "strategy-editor" && control.role === "summary" && /^高级规则/.test(control.label), via: "disclosure: strategy-editor-advanced（卡片改不了的规则，保存时原样带上；快照 state.strategyEditor.advanced）" },
  { match: control => control.group === "strategy-editor" && control.label === "回测明细", via: "disclosure: strategy-editor-backtest（最近 60 个交易日预览回测的逐笔；快照 state.strategyEditor.backtest）" },
  { match: control => control.group === "strategy-editor" && /^(存为草稿|保存中…)$/.test(control.label), via: "strategyEditor: {save: true}（= POST /api/strategy-plans，编辑时带 baseStrategyId 存为新版本）" },
  { match: control => control.group === "strategy-editor" && control.label === "上线运行…", via: "strategyEditor: {save: true} 后 dialog: {name: strategy-launch, open: true}（验证并上线仍由用户点；手机：存好回到我的策略并打开那条策略的上线确认）" },
  // 手机编辑卡 (2026-10-08): the four sections, the prefill's basis fold, a condition's bottom sheet.
  { match: control => control.group === "strategy-editor" && control.role === "button" && /^(品种与周期|开仓|平仓|仓位与时段)$/.test(control.label),
    via: "disclosure: {name: strategy-editor-section, id: basics | entry | exit | sizing}（手机编辑卡四段，一次展开一段）" },
  { match: control => control.group === "strategy-editor" && /^依据 · \d+ 笔/.test(control.label), via: "disclosure: strategy-editor-trades（手机编辑卡：预填依据的成交）" },
  { match: control => control.group === "strategy-editor" && /^改(做多|做空|平多|平空)第 \d+ 条/.test(control.label),
    via: "disclosure: {name: strategy-editor-condition, id: <组>:<第几条>}（手机编辑卡：从底部弹出的改条件面板）" },
  // 策略编辑卡第二版 (2026-10-08): the picker, the 流程图, ④'s 更多, the phone's 策略流程 block and every "?".
  { match: control => control.group === "strategy-editor" && /^(做多|做空|平多|平空)第 \d+ 条 · (指标|对象)$/.test(control.label),
    via: "disclosure: {name: strategy-editor-picker, id: <组>:<第几条>:<left|right>}（指标选择器：全部指标分类可搜索，好 = 写回草稿；快照 state.strategyEditor.fields）" },
  { match: control => control.group === "strategy-editor" && /：在表单里看$/.test(control.label),
    via: "strategyEditor: {focus: trigger | entryLong | entryShort | action | stop | take | … | limits}（流程图方块：表单定位到这一块；快照 state.strategyEditor.flow / focus）" },
  { match: control => control.group === "strategy-editor" && control.role === "summary" && /^更多：浮盈加仓/.test(control.label),
    via: "disclosure: strategy-editor-sizing-more（④ 的更多：浮盈加仓、冷却、每个方向次数、单笔预算、当日亏损上限）" },
  { match: control => control.group === "strategy-editor" && control.role === "button" && control.label === "策略流程",
    via: "disclosure: {name: strategy-editor-section, id: flow}（手机编辑卡的策略流程块）" },
  { match: control => control.role === "button" && /的说明$/.test(control.label), via: "disclosure: {name: help-tip, id: 说明的 key}（“?”说明浮层；快照 state.disclosures 里 help-tip:<key>）" },
  { match: control => control.group === "strategy-editor-backtest", via: "disclosure: strategy-editor-backtest（手机编辑卡：点底部回测四个数弹出的回测明细；快照 state.strategyEditor.backtest）" },
  { match: control => control.group === "strategy-editor-condition",
    via: "改条件面板（disclosure: strategy-editor-condition 开合；左边 / 比较 / 右边与好 = 写回草稿，AI 用 strategyEditor {ask: 一句话} 改条件；快照 state.strategyEditor.fields）" },
  { match: control => control.group === "strategy-editor",
    via: "编辑卡表单项：AI 用 strategyEditor {ask: 一句话} 改（改到的字段高亮），保存走 POST /api/strategy-plans（strategyEditor {save}）；快照 state.strategyEditor.fields" },
  { match: /^新建策略$/, via: "strategyEditor: {new: true}（我的策略：打开策略编辑卡，有成交时按习惯预填）" },
  // 我的策略's empty state and 从模板开始 ▾ (2026-10-08): every entry opens the 编辑卡 from that start.
  { match: control => control.label === "从模板开始" || /^用这个：/.test(control.label), via: "strategyEditor: {new: true, start: template:<模板id>}（模板 id 见 GET /api/strategy-templates）" },
  { match: control => control.group === "strategy-starts" && control.label === "从空白开始", via: "strategyEditor: {new: true, start: blank}" },
  { match: control => control.group === "strategy-starts" && control.label === "按我的习惯", via: "strategyEditor: {new: true, start: habits}（成交不够时不可点；够不够见 GET /api/strategy-plans/prefill 的 sample）" },
  { match: control => control.group === "strategy-starts" && /^从模板开始，/.test(control.label), via: "滚到下面的常用模板（每个“用这个”= strategyEditor {new: true, start: template:<id>}）" },
  { match: /^继续编辑：/, via: "strategyEditor: {open: true}（接着改没存的草稿）" },
  // 统一提醒 (#932, 2026-10-15; 2026-10-25 the right rail's 提醒 panel): the chart toolbar's bell, the panel / the phone's 提醒
  // page with its three tabs and AI 首页's 最新提醒 (before the generic rules: 新建 / 全部已读 / 不用 mean the center's things there).
  { match: /^提醒记录（\d+ 条未读）$/, via: "reminderCenter: {open: true | false}（K 线工具条的铃铛：打开提醒面板的提醒记录页签，N 是未读数；快照 state.reminderCenter）" },
  { match: /^(打开|收起)提醒面板$/, via: "panel: reminders / null（右侧栏“提醒”；页签 reminders: {tab}，快照 state.reminders）" },
  { match: /^关闭提醒面板$/, via: "panel: null" },
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && control.role === "tab" && /^(我的提醒|通用提醒|提醒记录)$/.test(control.label),
    via: "reminders: {tab: mine | general | history}（快照 state.reminders.tab）" },
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && control.label === "返回 AI 首页", via: "reminderCenter: {open: false}（手机提醒页也可 mobilePage: null）" },
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && control.role === "radio" && /^(未读|全部) ?\d+$/.test(control.label),
    via: "reminderCenter: {filter: unread | all}（快照 state.reminderCenter.filter / unread / today）" },
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && /^看今天全部 \d+ 条$/.test(control.label), via: "reminderCenter: {filter: all}" },
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && control.label === "继续加载当天提醒",
    via: "reminderCenter: {more: true}（当天超过一次读出的 500 条时读下一批；快照 state.reminderCenter.moreToday / today，#932 R5）" },
  // 条件提醒 (第 6 批 2026-10-25): 只提醒 strategies on 我的提醒; made in the editor card's 提醒模式, run by the strategy routes.
  { match: control => control.group === "reminders:conditions" && control.label === "新建提醒", via: "strategyEditor: {new: true, reminder: true}（打开策略编辑卡的提醒模式）" },
  { match: control => control.group === "reminders:conditions" && control.label === "编辑", via: "strategyEditor: {edit: <提醒 id>}（state.myReminders.items[].id）" },
  { match: control => control.group === "reminders:conditions" && /^(开始|继续)$/.test(control.label), via: "POST /api/strategy-plans/{id}/validate + POST /api/strategies/{runId}/monitor/start {signalAction: alert}" },
  { match: control => control.group === "reminders:conditions" && control.label === "暂停", via: "POST /api/strategies/{runId}/monitor/pause" },
  { match: control => control.group === "reminders:conditions" && /^删除提醒：.+$/.test(control.label), via: "（在跑的先 monitor/pause）DELETE /api/strategy-plans/{id}" },
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && control.label === "新建", via: "disclosure: reminder-alert-new（到价提醒 · 生效中的新建表单，当前合约）" },
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && / [A-Z]+\.[A-Za-z0-9]+$/.test(control.label), via: "symbol（到价提醒 · 生效中的合约名：切到该合约）" },
  { match: control => control.group === "mobile-latest-reminders" && control.label === "全部提醒", via: "reminderCenter: {open: true}（AI 首页 · 最新提醒的“全部 ›”）" },
  // 手机版新结构 (2026-10-06): AI 首页's 今天概况 / 今日判断 / bell, the 策略 tab and the 回测结果页
  // (before the desktop rules: 看图表 and 上线运行 mean other things there).
  { match: control => control.group === "mobile-ai" && /^持仓/.test(control.label), via: "mobilePage: positions（AI 首页 · 今天概况的持仓格）" },
  { match: control => control.group === "mobile-ai" && /^今天提醒/.test(control.label), via: "reminderCenter: {open: true}（AI 首页 · 今天提醒格：进提醒页；条数在快照 state.reminderCenter.today / unread）" },
  { match: control => control.group === "mobile-ai" && / (今日判断|暂无|读取中)$/.test(control.label), via: "symbol + mobileTab: research（今日判断胶囊：切到该品种主力合约并打开调研；判断还没读回时显示“读取中”）" },
  { match: /^看提醒(（\d+ 条未读）)?$/, via: "reminderCenter: {open: true}（AI 首页右上角铃铛进提醒页；未读数在快照 state.reminderCenter.unread / state.mobileUnread）" },
  { match: control => control.group === "mobile-strategy" && /^(暂停|继续运行|正在暂停…|正在继续…)$/.test(control.label), via: "POST /api/strategies/{id}/monitor/pause | …/monitor/start（策略卡的暂停 / 继续运行，与电脑运行中卡同一接口）" },
  { match: control => control.group === "mobile-strategy" && control.label === "看图表", via: "symbol + duration + mobileTab: market（策略卡：切到它的合约与周期看盘）" },
  // 手机版 策略 (第八批, 2026-10-08): 正在运行 / 我的策略, 停止并平仓 and 跟 AI 说…….
  { match: control => control.group === "mobile-strategy-tabs" && /^(正在运行|我的策略)( \d+)?$/.test(control.label), via: "mobileStrategyTab: running | mine（快照 state.mobileStrategyTab）" },
  { match: control => control.group === "mobile-strategy" && control.label === "停止并平仓", via: "卡内二次确认（确认后 = POST /api/strategies/{id}/monitor/stop {closePositions: true}，出平仓卡等确认，从不直接下单）" },
  { match: control => control.group === "mobile-strategy" && /^(确认停止并平仓|取消)$/.test(control.label), via: "POST /api/strategies/{id}/monitor/stop {closePositions: true} / 取消不发请求" },
  // 手机编辑卡 (2026-10-08): 我的策略 cards' 编辑 / 回测 / 上线….
  { match: control => control.group === "mobile-strategy" && control.label === "编辑", via: "strategyEditor: {edit: strategyId}（整屏策略编辑卡，存为新版本）" },
  { match: control => control.group === "mobile-strategy" && /^(回测|回测中…)$/.test(control.label), via: "POST /api/strategies/{id}/backtest（规则回测，与电脑策略卡同一接口；结果是卡上的最近回测）" },
  { match: control => control.group === "mobile-strategy" && control.label === "上线…", via: "卡内确认（确认后 = POST /api/strategy-plans/{id}/validate + /api/strategies/{id}/monitor/start，执行方式固定出卡确认）" },
  { match: control => control.group === "mobile-strategy" && /^(确认上线|正在验证并上线…)$/.test(control.label),
    via: "POST /api/strategy-plans/{id}/validate + /api/strategies/{id}/monitor/start {signalAction: confirm}（与回测结果页的上线同一校验）" },
  { match: control => control.group === "mobile-strategy" && control.label === "看运行中", via: "mobileStrategyTab: running" },
  { match: control => control.group === "mobile-strategy" && control.label === "发送", via: "chat:send（跟 AI 说……：workspaceContext.focus = {kind: strategy_edit, id: 选中的策略}）" },
  { match: control => control.group === "mobile-strategy" && control.role === "button", via: "chat:send 的 focus（点一张策略卡＝把它设为“跟 AI 说”的上下文；卡片内容读 GET /api/strategy-plans 与 …/monitor）" },
  { match: /^返回策略$/, via: "mobilePage: null（回测结果页左上角返回）" },
  { match: control => control.group === "mobile-lab" && /^(选用这一组|正在复核并保存…)$/.test(control.label), via: "POST /api/strategy-labs/{labId}/choose {variantId}（服务端先复核回测再存为我的策略；快照 state.mobileLab.chosen）" },
  { match: control => control.group === "mobile-lab" && /^(上线运行|正在验证并上线…|已上线)$/.test(control.label),
    via: "POST /api/strategy-plans/{id}/validate + POST /api/strategies/{executionId}/monitor/start {signalAction: confirm}（手机上线固定出卡确认，先查 GET /api/ai-execution 是否停止）" },
  { match: control => control.group === "mobile-lab" && control.label === "问 AI", via: "chat:send（以用户消息问这次回测的这一组）" },
  // 多 AI 研讨 (2026-10-11; its own 辩论 page 2026-10-24; before the 快讯 and phone 调研 rules, which would take these
  // buttons): the 研讨 entries, the 已研讨 tag and the page's own controls (group panel:debate / research-debate, on the
  // computer and the phone).
  { match: control => /^研讨：/.test(control.label) && ["news:flash", "mobile-research"].includes(control.group ?? ""),
    via: "researchDebate: {start: 事件 id}（重要快讯行的“研讨”：同一事件同一品种当天只辩一次，已辩过就打开结果；事件 id 是 GET /api/research/flash 的 events[].id，快照 state.researchDebate）" },
  { match: control => control.label === "打开这条快讯的研讨结果",
    via: "researchDebate: {open: 研讨 id}（快讯行的“已研讨”标签；GET /api/research/debates?events=事件 id 列出当天的研讨）" },
  { match: control => control.label === "让 AI 们辩一轮" && ["panel:news", "mobile-research"].includes(control.group ?? ""),
    via: "researchDebate: {start: desk}（今日判断旁：辩今日议题，今天已辩过且资料没变时打开已有结果）" },
  { match: control => control.group === "research-debate" && /^返回(快讯|调研)$/.test(control.label), via: "researchDebate: {close: true}（手机辩论页的返回）" },
  { match: control => control.group === "research-debate" && /^辩今日议题/.test(control.label), via: "researchDebate: {start: desk}（辩论页还没有辩论时：辩正在看的品种的今日议题）" },
  { match: control => control.group === "research-debate" && /^打开 .+的辩论（.+）$/.test(control.label),
    via: "researchDebate: {open: 研讨 id}（以前的辩论里的一格；GET /api/research/debates?product= 列出，快照 state.researchDebate.recent）" },
  { match: control => control.group === "research-debate" && control.label === "再辩一轮", via: "researchDebate: {rerun: true}（= POST /api/research/debates {force: true}：新跑一次，计入当日次数）" },
  { match: control => control.group === "research-debate" && /^依据 /.test(control.label),
    via: "disclosure: debate-sources（句子里的“名称 · 日期”标签：展开资料来源并定位那一条；手机在 debate-sheets 的底稿与来源里；数据同 GET /api/research/debates/{id} 的 facts[]）" },
  { match: control => control.group === "research-debate" && /^(展开|收起)(五份底稿|资料来源|研讨战绩|研讨记录|底稿与来源|裁判怎么取舍)$/.test(control.label),
    via: "disclosure: debate-sheets / debate-sources / debate-stats / debate-log / debate-judge（辩论结果页底部的折叠；快照 disclosures.debate-*）" },
  { match: control => control.group === "research-debate" && /^(展开|收起)(技术面|供需基本面|资金持仓|新闻宏观)分析师的报告$/.test(control.label),
    via: "disclosure: debate-analyst-technical / supply / flow / news（写完的分析师报告：推理、要点与全网补充的资料；数据同 GET /api/research/debates/{id} 的 analysts[]）" },
  { match: control => control.group === "research-debate" && control.label === "在对话里接着问", via: "chat:send（以用户消息就这次研讨接着问，带研讨 id；详情读 GET /api/research/debates/{id}）" },
  // 新闻改版 (2026-10-07 第六批): a merged event's reports and the folded 次要消息 (快讯 / 资讯 / 手机调研; before the
  // phone's 调研 chip rule, which takes any other button there).
  { match: control => control.group === "news:flash" && /^(展开|收起) \d+ 家报道$/.test(control.label),
    via: "disclosure: {name: flash-event, id: 事件 id}（快讯一个事件的各家报道与原文链接；事件在 GET /api/research/flash 的 events[]，快照 disclosures.flash-event 与 state.news.flash.events）" },
  { match: /^(展开|收起) \d+ 家报道$/, via: "disclosure: {name: news-event, id: 事件 id}（资讯页一个事件的各家报道；GET /api/research/news 的 events[]）" },
  { match: control => (control.group === "news:flash" || control.group === "mobile-research") && /^(展开|收起)次要消息$/.test(control.label),
    via: "disclosure: flash-minor（快讯 / 手机调研折叠的次要消息：影响弱且方向不明的事件；手机上连同其他与无关一起；快照 disclosures.flash-minor 与 state.news.flash.minor / minorReports）" },
  // 英文原文折叠 (2026-10-11): a translated headline's 原文 toggle (before the phone's 调研 chip rule).
  { match: control => control.role === "button" && /^原文 (展开|收起)$/.test(control.label),
    via: "disclosure: {name: flash-orig / news-orig, id: 条目 / 报道 / 事件 id}（翻译过的英文标题的原文折叠；快照 disclosures.flash-orig:<id> / news-orig:<id>）" },
  { match: /^(展开|收起)次要消息$/, via: "disclosure: news-minor（资讯页折叠的次要报道；快照 disclosures.news-minor 与 state.news.news.minor）" },
  // 调研页改版 (2026-10-07 第七批; before the phone's 调研 chip rule): 今日判断's 重新判断, the key figures and the reasons'
  // citation numbers (both open 资料来源 at their fact), the four folds and the 新闻面 line's link to the 快讯 tab.
  { match: control => control.group === "panel:news" && /^(重新判断|按最新消息重新判断)$/.test(control.label),
    via: "news: {deskRejudge: true}（= POST /api/research/desk/refresh：重读事实，判断满 1 小时才重算、当日次数照旧；回执与限制原因在快照 state.news.desk.rejudge，可能过时提示在 state.news.desk.stale）" },
  { match: control => control.group === "mobile-research" && control.label === "重新判断",
    via: "POST /api/research/desk/refresh（手机调研页“可能过时”提示里的重新判断，与电脑 news: {deskRejudge} 同一接口与限制，回执 rejudge.message）" },
  { match: control => control.group === "panel:news" && (/，看来源$/.test(control.label) || /^依据 \d+(，生成后已更新)?$/.test(control.label)),
    via: "news: {deskSource: 事实 id}（关键数据格 / 理由后的小编号：展开资料来源并高亮那一条；tiles[].ref 与 judgment.points[].basis 是 GET /api/research/desk 的事实 id，快照 state.news.desk.highlight）" },
  { match: control => control.group === "mobile-research" && /，看来源$/.test(control.label),
    via: "disclosure: desk-sources（手机调研页的关键数据格：展开资料来源并定位那一条；数据同 GET /api/research/desk 的 tiles[]）" },
  { match: control => (control.group === "panel:news" || control.group === "mobile-research") && /^(展开|收起)(基本面|宏观|相关报道|资料来源)$/.test(control.label),
    via: "disclosure: desk-fundamental / desk-macro / desk-reports / desk-sources（调研页底部四个折叠；快照 disclosures.desk-*，数据同 GET /api/research/desk 的 facts[]）" },
  { match: control => control.group === "panel:news" && control.label === "去快讯看 →", via: "news: {tab: flash, flashFilter: product}（新闻面一行链到快讯页当前品种）" },
  // 手机版第二期 (2026-10-04): the four tab pages and the full-screen pages (before the desktop rules,
  // whose labels — 成交, 下单 — the phone reuses for other things).
  { match: control => control.group === "mobile-research" && control.role === "button" && control.label === "添加自选品种", via: "dialog: contract-drawer（调研页“+ 自选”：合约列表里点星标加自选，自选清单是 preference: watchlist.products）" },
  { match: control => control.group === "mobile-research" && control.role === "button" && control.label === "就这份判断问 AI", via: "chat:send（以用户消息发出对当前品种今日判断的追问）" },
  { match: control => control.group === "mobile-research" && control.role === "button" && !/^(问 AI|详情)/.test(control.label), via: "symbol（调研页品种胶囊：切到该品种主力合约，调研跟随当前合约；快照 state.symbol）" },
  { match: /^选择合约：/, via: "dialog: contract-drawer（手机版看盘的合约标题，打开合约列表）" },
  { match: /^设提醒（\d+ 条生效）$/, via: "disclosure: price-alerts（手机版看盘的设提醒：当前合约的到价提醒浮层；电脑在提醒面板“我的提醒”的到价提醒 · 生效中）" },
  { match: control => control.group === "mobile-market" && control.label === "下单", via: "mobilePage: order（手机版下单页；确认才下单，快照 state.mobilePage）" },
  { match: control => control.group === "mobile-market-list", via: "symbol（自选 · 持仓标记行：切图表合约）" },
  // 手机看盘第二批 (2026-10-09): 报价 / 多图 / 单图, the 报价 list, the 多图 cells and the 副图设置 sheet.
  { match: control => control.group === "mobile-market-view", via: "mobileMarket: {view: quotes | multi | single}（看盘顶部切换；快照 state.preferences.ui.mobileMarket.view）" },
  { match: control => control.group === "mobile-market" && control.label === "副图设置", via: "mobileMarket: {subPanes: true | false}（单图周期行的 ⚙；快照 disclosures.mobile-subpanes）" },
  // 手机画线 (2026-10-09): ✎ on the single chart's period row is the computer's drawing toolbar as a sheet.
  { match: control => control.group === "mobile-market" && control.label === "画线工具", via: "disclosure: chart-drawing-toolbar（手机单图的 ✎ 底部面板；快照 state.chartDrawings.toolbar）" },
  // The sheet (group chart:drawing-toolbar), the bar under the chart while placing (chart:drawing-hint) and the selected
  // line's bar (chart:drawing-style) with its one-at-a-time style rows; the rows' radios are the computer's style controls.
  { match: control => control.group === "chart:drawing-toolbar" && control.label === "完成", via: "disclosure: {name: chart-drawing-toolbar, open: false}（收起 ✎ 面板）" },
  { match: control => control.group === "chart:drawing-toolbar" && control.role === "button"
      && /^(趋势线|射线|直线|水平线|水平射线|垂直线|平行通道|折线|价格区间|矩形区间|黄金分割|百分比线|江恩八线|矩形|圆或椭圆|三角形|文字|箭头|价格标签|测量)$/.test(control.label),
    via: "disclosure: {name: chart-drawing-tool, id: <工具名>, open: true}（手机 ✎ 面板的工具；快照 state.chartDrawings.tool）" },
  { match: control => control.group === "chart:drawing-toolbar" && /^不画了（.+）$/.test(control.label), via: "disclosure: {name: chart-drawing-tool, id: <工具名>, open: false}" },
  { match: control => control.group === "chart:drawing-toolbar" && control.label === "磁吸", via: "disclosure: chart-drawing-magnet（= preference chart.drawingMagnet）" },
  { match: control => control.group === "chart:drawing-toolbar" && /^显示单独隐藏的 \d+ 条$/.test(control.label), via: "PUT /api/market/drawings/{drawing_id} {style: {hidden: null}}" },
  { match: control => control.group === "chart:drawing-hint" && control.label === "取消", via: "disclosure: {name: chart-drawing-tool, id: <工具名>, open: false}（结束放置）" },
  { match: control => control.group === "chart:drawing-hint" && control.label === "完成", via: "POST /api/market/drawings（折线点够两点后结束放置；AI 用 create_chart_drawing）" },
  { match: control => control.group === "chart:drawing-style" && control.label === "完成", via: "disclosure: {name: chart-drawing-style, id: <线 id>, open: false}（取消选中）" },
  { match: control => control.group === "chart:drawing-style" && control.label === "改整条", via: "disclosure: {name: chart-drawing-style, id: <线 id>}（重新选中 = ±1 跳动整条线；快照 state.chartDrawings.handle）" },
  { match: control => control.group === "chart:drawing-style" && /^(上移|下移) 1 跳$/.test(control.label), via: "PUT /api/market/drawings/{drawing_id} {points}（价格按最小变动价位 ±1 跳；AI 用 update_chart_drawing）" },
  { match: control => control.group === "chart:drawing-style" && /^(颜色|粗细|线型)$/.test(control.label),
    via: "disclosure: {name: mobile-drawing-style-row, id: color|width|dash}（展开一排；快照 state.disclosures 里 mobile-drawing-style-row:<id>）" },
  { match: control => control.group === "mobile-market" && /^(KDJ|MACD|RSI|持仓量|CCI|成交量)$/.test(control.label),
    via: "mobileMarket: {subPane}（单图下显示哪个副图，左右滑同效；快照 preferences.ui.mobileMarket.subPane）" },
  { match: control => control.group === "mobile-quotes" && /^(自选|主力|上期所|大商所|郑商所|中金所|广期所|能源中心|外盘)$/.test(control.label), via: "mobileMarket: {board}（报价板块，左右滑同效；外盘＝FOREIGN）" },
  { match: control => control.group === "mobile-quotes" && control.label === "报价列设置", via: "disclosure: mobile-quote-columns（报价列面板）" },
  { match: control => control.group === "mobile-quotes" && /^(最新|涨跌幅|涨跌|持仓量|买价|卖价|成交量|日增仓)$/.test(control.label), via: "mobileMarket: {columns}（报价数值列 1–4 个）" },
  { match: control => control.group === "mobile-quotes" && control.label === "完成", via: "disclosure: {name: mobile-quote-columns, open: false}" },
  { match: control => control.group === "mobile-quotes" && /^看 .+ 单图$/.test(control.label), via: "symbol + mobileMarket: {view: single}（报价行：进该合约单图）" },
  { match: control => (control.group === "mobile-quotes" || control.group === "mobile-multi") && control.label === "登录", via: "loginOpen: true（游客：报价 / 多图登录后看）" },
  { match: control => control.group === "mobile-multi" && /^(4 合约|同合约 4 周期|6 图)$/.test(control.label), via: "mobileMarket: {layout: four | periods | six}" },
  { match: control => control.group === "mobile-multi" && /^改第 \d 格：/.test(control.label), via: "disclosure: {name: mobile-chart-cell, id: 格子序号 0–5}（改这一格的合约与周期）" },
  { match: control => control.group === "mobile-multi" && /^看 .+ 单图$/.test(control.label), via: "symbol + duration + mobileMarket: {view: single}（多图格子：进它的单图）" },
  { match: control => control.group === "mobile-multi" && control.label === "完成", via: "disclosure: {name: mobile-chart-cell, open: false}" },
  { match: control => control.group === "mobile-multi", via: "mobileMarket: {cell: {index, symbol?, duration?}}（格子面板里的合约（自动＝symbol null）与周期）" },
  { match: control => control.group === "mobile-subpanes" && control.label === "完成", via: "mobileMarket: {subPanes: false}" },
  { match: control => control.group === "mobile-subpanes" && /^(KDJ|MACD|RSI|持仓量|CCI|成交量)\d?$/.test(control.label), via: "preference: chart.subPaneList（副图 0–3 个，电脑同一份）" },
  { match: control => control.group === "mobile-subpanes" && /^(均线|布林|SAR|无)$/.test(control.label), via: "preference: chart.maLines[].enabled / chart.boll.enabled / chart.sar.enabled（主图指标）" },
  { match: control => control.group === "mobile-subpanes" && control.label === "均线、布林与 SAR 参数", via: "disclosure: mobile-subpanes-overlays（均线、布林、SAR 参数折叠）" },
  { match: /^关闭下单$/, via: "mobilePage: null（关闭手机版下单页）" },
  { match: control => control.role === "summary" && /^止损 \/ 止盈/.test(control.label), via: "disclosure: ticket-protection（手机版下单页的止损 / 止盈折叠；价格本身随委托 body 走）" },
  { match: control => control.group === "mobile-notify" && /^(全部|成交|AI 简报)$/.test(control.label), via: "mobileNotifyFilter: all|fill|brief（AI 首页时间线的筛选，原通知页；快照 state.mobileNotifyFilter）" },
  { match: control => control.group === "mobile-notify-list", via: "mobileTab: chat（对话里的判断与简报、确认卡、回测：打开对话并定位到该卡）/ mobilePage: positions + positionsTab: fills（成交）/ mobileTab: research（调研台判断）" },
  { match: control => control.group === "mobile-me" && /^提醒设置/.test(control.label), via: "reminders: {tab: general}（旧 settingsPage: notify 同样到这里；我的 → 提醒设置：提醒页的通用提醒页签）" },
  { match: control => control.group === "mobile-push" && /^提醒哪些事/.test(control.label), via: "reminders: {tab: general}（锁屏提醒页 → 提醒设置：提醒页的通用提醒页签，推哪几类、显示金额和手数）" },
  { match: control => control.group === "mobile-me" && /^(持仓与委托|复盘摘要|交易账户|AI 简报推送|锁屏提醒|添加到手机桌面|外观与字号|AI 与记忆|帮助)/.test(control.label),
    via: "mobilePage: positions|review|briefs|push|install|accounts|appearance|memory|help（我的各行；briefs 是 AI 简报推送页，push 是锁屏提醒页，install 是安装到桌面引导，accounts/memory/appearance 打开设置对应页，help 打开使用说明卡）" },
  // 锁屏提醒 (2026-10-06): the page closes with mobilePage: null; 先放到主屏幕 opens the install guide.
  { match: control => control.surface === "mobile-push" && control.label === "知道了", via: "mobilePage: null（关闭锁屏提醒页；快照 state.pushSupport / state.pushSubscribed）" },
  { match: /^教我放到主屏幕$/, via: "mobilePage: install（iPhone 在 Safari 标签页里：先放到主屏幕再开锁屏提醒）" },
  { match: /^\d+ 台$/, via: "disclosure: push-devices（提醒面板 → 通用提醒 → 锁屏推送 · 已开的设备列表，先 reminders: {tab: general}；设备本身读 GET /api/app/push）" },
  // 安装到桌面引导 (2026-10-06): closing is mobilePage: null; the environment is in state.installEnvironment.
  { match: control => control.group === "mobile-install" && /^(关闭安装引导|以后再说|知道了)$/.test(control.label), via: "mobilePage: null（关闭安装引导；快照 state.installEnvironment 是识别出的浏览器环境）" },
  // AI 简报订阅 (2026-10-05): 通知 · AI 简报's product chips, 设置 → 通知's 订阅品种 entry and the 添加品种 picker.
  { match: control => control.group === "mobile-notify-products", via: "mobileNotifyProduct: <交易所>.<品种>|null（时间线 · AI 简报下按品种筛，只列订阅与出现的品种；快照 state.mobileNotifyProduct）" },
  { match: /^(AI 简讯的)?订阅品种/, via: "disclosure: brief-subscriptions（提醒面板 → 通用提醒 · AI 简讯行的订阅品种链接，展开生成简讯与订阅品种区块，先 reminders: {tab: general}；快照 disclosures.brief-subscriptions）" },
  { match: control => control.group === "订阅品种" && /^(添加品种|收起)$/.test(control.label), via: "disclosure: brief-add（订阅品种下的添加品种选择区；手机在 mobilePage: briefs，电脑在 disclosure: brief-subscriptions 里）" },
  { match: control => control.group === "mobile-me" && /^到价提醒/.test(control.label), via: "mobileTab: market + disclosure: price-alerts（我的 → 到价提醒 N 条）" },
  { match: /^返回我的$/, via: "mobilePage: null（复盘摘要页左上角返回）" },
  // 我的流程 (2026-10-06): 设置 → 记忆's routine rows — 删 / 取消 open and close the in-place confirmation,
  // 运行 is the routine's name sent as the user's message, carrying the routine's id (#522).
  { match: /^(删|取消删除)「.+」$/, via: "disclosure: routine-delete（id = 流程 id；设置 → 记忆 → 我的流程某一行的删除确认，先 settingsPage: memory；快照 disclosures.routine-delete:<id>）" },
  { match: /^运行「.+」$/, via: "POST /api/routines/{routine_id}/run（与设置里的运行一样按流程 id 运行，对话里显示“跑一下<名称>”；agent 须带用户原话 quote；快照 routines.items）" },
  // 手机版游客: AI / 策略 / 我的 are the 登录后使用 page; its button is the login dialog.
  { match: control => control.group === "mobile-signin" && control.label === "登录", via: "loginOpen: true（游客窗口的登录后使用页：打开统一登录框；标签本身是 mobileTab: ai|strategy|me）" },
  { match: /^钉住合约$/, via: "dialog: contract-drawer（钉住模式）/ pinnedSymbols" },
  { match: /^复盘$/, via: "view: review" },
  { match: /^回放$/, via: "dialog: replay-setup" },
  { match: /^(开始语音通话|结束语音通话|AI 动态)/, via: "disclosure: voice-call / aiActivityCard" },
  { match: control => control.role === "button" && control.group === "market:contract", via: "dialog: contract-drawer（行情栏合约名按钮，稳定标识 market:contract）" },
  { match: /^(分时|1分|3分|5分|15分|30分|1小时|日线|周线)$/, via: "chartView（分时）/ duration（周期，30 分与周线 2026-10-02 起在白名单）" },
  { match: /^更多周期(（当前 .+）)?$/, via: "disclosure: chart-periods-more（周期行放不下时的“更多”，选中收起的周期时按钮写它；快照 disclosures.chart-periods-more，选周期仍是 duration）" },
  { match: /查看交易时间$/, via: "dialog: market-session" },
  { match: /^(图表说明|查看说明|图表与下单说明)$/, via: "dialog: {name: help, topic}" },
  { match: /^调整(价格图和(KD|MACD|RSI|持仓量|CCI|成交量)|(KD|MACD|RSI|持仓量|CCI|成交量)副图和(KD|MACD|RSI|持仓量|CCI|成交量))副图高度$/, via: "chartSizes（按格存储 paneSizes，快照仍报旧形状）" },
  { match: /^调整辅助区宽度$/, via: "dockWidth" },
  { match: /^(展开|收起|打开)持仓与(委托|订单)$/, via: "positionsBar" },
  { match: /^查看持仓 /, via: "panel: positions（持仓带上的持仓胶囊）" },
  { match: /^查看全部$/, via: "panel: positions（1.6.6 起是持仓面板；dialog: positions-drawer 保留为别名）" },
  { match: /^(开仓|平仓|目标持仓)$/, via: "ticketMode" },
  { match: /^(打开|收起)对话$/, via: "panel: ai / null（旧的 aiPage 命令仍映射到这里）" },
  { match: /^(打开|收起)策略面板$/, via: "panel: strategy / null" },
  { match: /^(打开|收起)持仓面板$/, via: "panel: positions / null" },
  // 建议问题 (2026-10-05): 0–2 questions the AI writes (POST /api/agent/suggestions); a click only fills the input.
  { match: control => control.group === "chat-examples", via: "chat:send（建议问题：AI 按行情与上下文生成 0–2 条，点一下只把这句填进输入框，不发送；AI 直接发同一句即可；文本在快照 text 与 controls 里）" },
  { match: control => control.surface.startsWith("ai-positions") && /^(全部|手动|AI|AI 建议|AI 自主|策略|止损|强平)$/.test(control.label), via: "fillsSource: all|manual|card|ai|strategy|stop|liquidation（持仓面板成交页来源筛选）" },
  { match: /^(开始|结束)语音通话$/, via: "disclosure: voice-call（通话栏与 AI 球共用同一路通话）" },
  { match: /^(静音|取消静音)$/, via: "disclosure: voice-mute" },
  // 离线状态的重试 (2026-10-11): shown only after a failed / empty read, each with its own command.
  { match: control => control.label === "重试" && control.surface.startsWith("dialog-contract-drawer"),
    via: "contractCatalogRetry: true（目录读取失败或为空时；快照 state.contractCatalog.canRetry）" },
  { match: control => control.label === "重试" && control.surface.startsWith("dialog-help"),
    via: "help: {retry: true}（说明读取失败时；快照 state.help.canRetry）" },
  { match: /^重试$/, via: "disclosure: voice-call（通话出错后重试）" },
  { match: control => control.group === "call-bar" && control.label === "关闭", via: "disclosure: {voice-call: false}（通话出错后关闭错误条，回到未通话）" },
  { match: /^暂停$/, via: "策略运行横幅 / 运行中卡：POST /api/strategies/{id}/monitor/pause" },
  { match: /^继续$/, via: "运行中卡：POST /api/strategy-plans/{id}/validate（实例自己的合约与周期）+ /api/strategies/{id}/monitor/start" },
  { match: / 的更多操作$/, via: "disclosure: strategy-actions（id=library:<策略id> 或 replay:<策略id>）" },
  { match: control => control.role === "menuitem" && control.label === "删除策略", via: "disclosure: strategy-delete-confirm（仅打开确认，不删除）" },
  // 侧栏重设计第三期 (2026-09-25): 执行方式、停止确认、上线对话框、对象级 AI 菜单.
  { match: control => control.surface.startsWith("ai-strategy") && /^(只提醒|出卡确认|自动下单)/.test(control.label) && control.role === "button", via: "disclosure: {name: strategy-signal-action, id: 实例id}（执行方式菜单）" },
  { match: control => control.role === "menuitemradio" && /^(只提醒|出卡确认|自动下单)/.test(control.label), via: "start_strategy_monitoring {strategyId, signalAction}（= POST /api/strategies/{id}/monitor/start；不经界面命令改，#932）" },
  { match: /^停止并平仓$/, via: "disclosure: {name: strategy-stop-confirm, id: 实例id}（二次确认）" },
  // 回撤停机 (2026-10-09): 已停止 · 手动恢复 的唯一主按钮（电脑运行中卡与手机策略卡）。
  { match: /^恢复运行$/, via: "POST /api/strategies/{id}/monitor/start {resetDrawdown: true}（只在 monitor.drawdown.canResume 时可点；普通 start 不重置）" },
  { match: /^只停止，不平仓$/, via: "POST /api/strategies/{id}/monitor/stop（回到没上线，不平已有持仓）" },
  { match: /^确认停止并平仓$/, via: "POST /api/strategies/{id}/monitor/stop {closePositions: true}（停止后出平仓卡等确认，从不直接下单）" },
  { match: /^问 AI 它为什么这么做$/, via: "chat:send（以用户身份发一句短问句，workspaceContext.focus 带上该运行实例）" },
  { match: /^AI：/, via: "disclosure: {name: ai-menu, id: strategy:/instance:/position:/order:…}（对象级 AI 菜单）" },
  { match: control => control.role === "menuitem" && /^(解释这条策略|解读当前信号|为什么没开仓|今天表现怎么样|建议调整参数|这笔持仓的风险|平仓建议（出卡）|为什么没成交|追单建议（出卡）)$/.test(control.label),
    via: "chat:send（以用户身份发一句短问句，workspaceContext.focus 带上该对象）" },
  // 规则回测 (策略积木·第一期 B, 2026-09-25): 详情的回测、结果卡与图表工具栏入口.
  { match: /^(回测|回测中…|排队中…|重跑|开始回测)$/, via: "strategyBacktest: {run: true}（= POST /api/strategies/{id}/backtest）" },
  { match: /^(区间 · 交易日|滑点)$/, via: "strategyBacktest: {tradingDays | slippageTicks}" },
  { match: /^(在图上标出买卖点|清除买卖点)$/, via: "strategyBacktest: {mark: true | false}" },
  { match: control => /^逐笔清单/.test(control.label) && control.role === "summary", via: "strategyBacktest: {trades}（详情）/ disclosure: {name: strategy-backtest-trades, id: 卡id}（对话卡）" },
  { match: control => control.surface === "dialog-strategy-launch" && control.role === "radio", via: "strategyLaunch: {signalAction}" },
  { match: control => control.surface === "dialog-strategy-launch" && /^(取消|关闭上线运行)$/.test(control.label), via: "dialog: {name: strategy-launch, open: false}" },
  { match: /^验证并上线$/, via: "POST /api/strategy-plans/{id}/validate + POST /api/strategies/{id}/monitor/start {signalAction, sessionId}（用户点；字段用 strategyLaunch 填）" },
  { match: /^(新对话|历史对话)$/, via: "chatSession: {new: true} / disclosure: chat-history" },
  // 说人话 (2026-09-25): each parameter's “?” note (结果页 · 来自模板的参数).
  { match: control => control.role === "button" && /是什么$/.test(control.label) && /^ai-strategy|strategy-template/.test(`${control.surface} ${control.group ?? ""}`),
    via: "disclosure: {name: strategy-param-help, id: 参数key}（参数旁的“?”说明）" },
  // 策略页改版第八批 (2026-10-08): two tabs, the strategy cards and the 结果页.
  { match: control => /^(打法|我的策略|正在运行)( · \d+)?$/.test(control.label) && control.group === "strategy-tabs", via: "strategyTab: playbooks | library | running（结果页是 detail；旧值 lab 打开结果页）" },
  // 打法本 (2026-10-25): the 策略 panel's 打法 tab — its 新建 form (disclosure playbook-editor) and every setup's 改 / 删.
  ...((): Rule[] => {
    const inPlaybooks = (control: Control) => control.group === "strategy-playbooks" || (control.group ?? "").startsWith("playbook:");
    return [
      { match: control => inPlaybooks(control) && control.label === "新建打法", via: "disclosure: {name: playbook-editor, open: true}（空白表单；保存走 POST /api/playbooks）" },
      { match: control => inPlaybooks(control) && /^(打法名字|入场规则|出场规则|止损规则|关联策略)$/.test(control.label),
        via: "POST /api/playbooks | PUT /api/playbooks/{id} 的 name / entryRules / exitRules / stopRules / strategyId" },
      { match: control => inPlaybooks(control) && control.label === "保存", via: "POST /api/playbooks（新建）| PUT /api/playbooks/{id}（改）" },
      { match: control => inPlaybooks(control) && control.label === "取消", via: "disclosure: {name: playbook-editor, open: false}" },
      { match: control => inPlaybooks(control) && control.label === "改", via: "GET /api/playbooks 读出后 PUT /api/playbooks/{id}" },
      { match: control => inPlaybooks(control) && /^(删|确认删除)$/.test(control.label), via: "DELETE /api/playbooks/{id}" },
    ];
  })(),
  { match: control => control.label === "打法" && control.role === "combobox", via: "POST /api/simulation/proposals {playbookId}（下单票开仓时的打法；GET /api/playbooks 列出可选的）" },
  { match: control => control.label === "这笔的打法" && control.role === "combobox",
    via: "PUT /api/account/trade-playbooks/{roundId} {playbookId} | DELETE（复盘里事后补标；复盘与 /api/account/pnl 的 trades[].playbookName、breakdown 随之变）" },
  { match: control => control.surface.startsWith("ai-strategy") && / · (运行中|已暂停|没上线|要补条件|验证中)$/.test(control.label), via: "strategy: {selectedStrategyId}（策略卡：打开它的结果页）" },
  { match: /^返回我的策略$/, via: "strategyTab: library（结果页）/ 策略编辑卡的“‹ 我的策略”：收起卡片、保留草稿（strategyEditor {open} 接着改）" },
  { match: /^跟 AI 说怎么改$/, via: "disclosure: strategy-ask-ai（结果页的输入框；发出去是 chat:send，focus = {kind: strategy_edit}）" },
  { match: /^发给 AI$/, via: "chat:send（workspaceContext.focus = {kind: strategy_edit, id: 策略}）" },
  { match: /^存着先不上线$/, via: "strategyTab: library（策略已存在我的策略里）" },
  { match: /^(比一比|重新比|正在算…)$/, via: "strategyLab: {run: true}（= POST /api/strategy-plans/{id}/compare，三组，界面默认本机算；快照 state.strategyLab）" },
  { match: control => control.role === "summary" && /^更多：/.test(control.label), via: "strategyDetails: {section: rules, open}（结果页底部：回测明细、完整规则、适用范围、原始想法）" },
  { match: /^(恢复模板默认|恢复中…)$/, via: "strategyTemplate: {restore: true}（结果页里来自模板的策略另存默认参数的新版本）" },
  { match: /^运行日志$/, via: "disclosure: {name: strategy-run-log, id: 实例id}" },

  { match: /^(打开|收起)模拟下单$/, via: "panel: order / null" },
  // 调研 (was 新闻, 2026-10-25): its 辩论 tab replaced the rail's own 辩论 page (panel: debate = news.tab debate).
  { match: /^(打开|收起)调研面板$/, via: "panel: news / null（辩论页签：panel: debate 或 news: {tab: debate}，快照 state.news.tab 与 state.researchDebate）" },
  { match: /^关闭调研面板$/, via: "panel: null" },
  { match: /^(固定阅读品种|跟随当前品种)$/, via: "news: {pinnedSymbol: 合约 | null}（快照 state.news.pinnedSymbol）" },
  // 快讯页签 (2026-10-03): the filter row, the product tags and the calendar's 展开全部.
  { match: control => control.group === "news:flash" && /^(当前品种|持仓与自选|宏观|全部)$/.test(control.label),
    via: "news: {flashFilter: product|watch|macro|all}（只给筛选也会切到快讯页；快照 state.news.flashFilter 与 state.news.flash.count）" },
  { match: control => control.group === "news:flash" && /^切到 .+ 主力合约$/.test(control.label), via: "symbol（快讯品种标签：该品种的主力合约，title 带合约代码）" },
  { match: control => control.group === "news:flash" && /^(展开全部 \d+ 条|收起)$/.test(control.label),
    via: "disclosure: flash-calendar（本周重要数据展开全部；快照 disclosures.flash-calendar 与 state.news.flash.calendarCount）" },
  // 新闻 AI 筛选 (2026-10-07): the 快讯 sort switch and the folded “筛掉了 N 条” row (快讯 / 资讯 / 手机调研).
  { match: control => control.group === "news:flash" && /^(按时间|按影响)$/.test(control.label),
    via: "news: {flashSort: time|impact}（只给排序也会切到快讯页；快照 state.news.flashSort 与 state.news.flash.sort）" },
  { match: control => (control.group === "news:flash" || control.group === "mobile-research") && /^(看筛掉了什么|查看|收起筛掉的)$/.test(control.label),
    via: "disclosure: flash-dropped（快讯被 AI / 排除词筛掉的条目按原因分组列出；快照 disclosures.flash-dropped 与 state.news.flash.dropped）" },
  { match: /^(看筛掉了什么|收起筛掉的)$/, via: "disclosure: news-dropped（资讯页被筛掉的报道；快照 disclosures.news-dropped 与 state.news.news.dropped）" },
  { match: /^语音复盘$/, via: "disclosure: voice-review（复盘页：open = 正在语音复盘）" },
  // 今日复盘 (2026-10-06): the block's day steps, its 怎么算的 fold and the conversation card's 看复盘.
  { match: /^(前一个交易日的复盘|后一个交易日的复盘)$/, via: "reviewDay: <交易日>（今日复盘标题旁的前一天 / 后一天；可选的交易日是 GET /api/account/daily-review 的 days；快照 review.dailyReview.tradingDay）" },
  // 习惯对照 (2026-10-25): its own 怎么算的 fold and the 最该看的几笔 rows (each selects that trade in 平仓记录).
  { match: control => control.group === "review:habits" && control.role === "summary" && control.label === "怎么算的",
    via: "disclosure: habits-how（习惯对照底部的算法说明；快照 disclosures.habits-how）" },
  { match: control => control.group === "review:habits" && control.role === "button" && / 手/.test(control.label),
    via: "review: {select: <成交 id>}（习惯对照“最该看的几笔”；快照 review.habits.picks 是这几笔的 id）" },
  { match: control => control.role === "summary" && control.label === "怎么算的", via: "disclosure: review-how（今日复盘底部的标签规则原话；快照 disclosures.review-how）" },
  { match: /^看复盘$/, via: "reviewDay: <交易日>（今日复盘卡：打开复盘页 / 手机复盘摘要页并切到那天）" },
  { match: /^(展开|收起)未关联的补充$/, via: "disclosure: unlinked-notes（复盘页末尾“未关联的补充（N）”，有这类补充时才有；快照 review.unlinkedNotes 是条数）" },
  { match: /^设置$/, via: "settingsOpen" },
  { match: /^收起AI$/, via: "panel: null" },
  { match: /^(编辑|完善策略)$/, via: "strategyEditor: {edit: strategyId}（结果页：在策略编辑卡里改这条，存为新版本）" },
  { match: control => control.surface.startsWith("ai-strategy") && control.role === "summary" && /^(适用范围|原始想法)/.test(control.label), via: "strategyDetails: {section: scope|idea}" },
  { match: control => /^(ai-(strategy|positions)|position-protection)/.test(control.surface) && /^(展开|收起)$/.test(control.label), via: "disclosure: protection-status / monitorCard: research" },
  { match: /^(查看历史|返回当前|查看更多操作|收起)$/, via: "disclosure: timeline-history / timeline-expanded / activity-history / activity-more" },
    { match: control => control.surface === "chat-history" && /当前|\d{4}\//.test(control.label), via: "chatSession: {sessionId}" },
  { match: control => control.role === "summary" && /^详情$/.test(control.label), via: "disclosure: card-details" },
  { match: /^发送给AI$/, via: "chat:send（外部 agent）" },
  { match: /^(收起AI|收起策略|收起持仓)$/, via: "panel: null" },
  { match: /^(持仓|委托|成交)( \((?:\d+|—)\))?$/, via: "positionsTab: positions|orders|fills" },
  { match: /^刷新账户$/, via: "GET /api/account（持仓面板“刷新账户”，读取即刷新）" },
  { match: /^平仓 [A-Z]+\.[A-Za-z0-9]+ (多|空)仓$/, via: "selectPosition" },
  { match: /^历史$/, via: "ordersHistory" },
  { match: /^合约筛选$/, via: "reviewFilter" },
  { match: /查看账户明细$/, via: "dialog: account-detail（复盘页）" },
  { match: /^手续费口径$/, via: "dialog: fee-hint（复盘页）" },
  { match: /^关闭(系统设置|交易时间|说明|合约列表|回放设置|账户明细|手续费口径)$/, via: "settingsOpen: false / dialog: {open: false}" },
  { match: /^(账户|AI 与对话|记忆|语音|图表|交易|提醒|界面|快捷键|数据与隐私|关于)$/, via: "settingsPage: account|ai|memory|voice|chart|trade|notify（提醒）|ui|shortcuts|data|about" },
  // 设置拆页 (2026-09-30): 一级页尾的 SettingsLink 进二级页，二级页头的 ‹ 返回键回一级页。
  { match: /^(打开)?(高级交易设置|判停与确认时限|指标参数与均线|约定、经验与最近变动|问题与建议)$/, via: "settingsPage: trade-advanced|voice-advanced|chart-advanced|memory-advanced|feedback（一级页尾的 SettingsLink，aria-label 带“打开”前缀；问题与建议在关于下）" },
  { match: /^返回(交易|语音|图表|记忆|关于)$/, via: "settingsPage: trade|voice|chart|memory|about（二级页头的返回键）" },
  // 问题与建议 (2026-10-06): the diagnostics preview fold; the form itself is exempt below.
  { match: control => control.surface.startsWith("settings-feedback") && control.role === "summary" && control.label === "预览诊断内容",
    via: "disclosure: feedback-diagnostics（附上基础诊断开着时才有；快照 disclosures.feedback-diagnostics，预览正文同邮件里的白名单诊断）" },
  { match: /^(深色|浅色|跟随系统)$/, via: "theme" },
  { match: /^(标准 100%|大字 110%|特大 120%)$/, via: "fontScale" },
  { match: /^(紧凑|标准|宽松)$/, via: "density" },
  { match: /^(左侧|右侧)$/, via: "dockSide" },
  { match: control => control.group !== "chart:context-menu" && /^(成交量|同步十字线|K 线最新价线)$/.test(control.label), via: "chartOption（副图的增删走 设置 → 图表 → 指标参数与均线 与图上编辑，同一份 chart.subPaneList；chartOption 的 kd/macd 名字仍可用）" },
  { match: /^快捷下单免确认/, via: "quickOrderConfirm" },
  // 设置 → 账户 (2026-10-08): the 实盘 switch (no futures company connected: stays off) and the 档位 fold.
  { match: control => control.role === "switch" && control.label === "实盘",
    via: "disclosure: live-trading（暂无期货公司接入：open: true 被拒并说明原因，开关保持关闭；快照 state.liveTrading＝{state: off, disabledReason}）" },
  { match: /^.+：(展开|收起)每日额度$/, via: "disclosure: plan-quota（档位名按钮：展开当日 token 与语音的额度和用量；快照 disclosures.plan-quota）" },
  // 设置重做第 2 步: account preference rows (`preference: {key, value}`; the snapshot reports `preferences`).
  { match: control => control.surface.startsWith("settings-") && /^(慢|正常|快)$/.test(control.label), via: "preference: voice.speed" },
  { match: /^通话中自动画图$/, via: "preference: voice.autoAnnotate（成交 / 简讯播报 2026-10-15 并进提醒面板 → 通用提醒的每类开关）" },
  { match: /^松开后直接发送$/, via: "preference: voice.pttSendOnRelease" },
  { match: /^默认周期$/, via: "preference: chart.defaultDuration" },
  { match: /^(蜡烛|美国线)$/, via: "preference: chart.candleStyle" },
  { match: /^(红涨绿跌|绿涨红跌)$/, via: "preference: chart.upDownColors" },
  { match: /^默认显示 AI 标注$/, via: "preference: chart.aiAnnotationsVisible" },
  { match: control => control.surface.startsWith("settings-") && /^(对手价|挂单价)$/.test(control.label), via: "preference: trade.defaultPriceIntent" },
  { match: control => control.surface.startsWith("settings-") && /^(自动|优先平今|优先平昨)$/.test(control.label), via: "preference: trade.closePreference" },
  { match: /^语音下单免口头确认$/, via: "update_preferences / PUT /api/preferences: trade.voiceOrderConfirm（开关取反：免确认 = false；不经界面命令改，#932）" },
  { match: /^AI 自主执行$/, via: "update_preferences / PUT /api/preferences: trade.aiAutonomy（关闭时 Runtime 拒绝 AI 自己下单，只出方案卡；不经界面命令改，#932）" },
  { match: control => control.surface.startsWith("settings-") && /^(简洁|详细)$/.test(control.label), via: "preference: chat.replyStyle" },
  { match: /^自动读屏$/, via: "preference: chat.autoReadScreen" },
  { match: /^让 AI 记住我$/, via: "preference: chat.memory（关闭后 Runtime 对记忆读写一律 409）" },
  { match: /^(Enter|Ctrl \/ Cmd \+ Enter)$/, via: "preference: chat.sendKey" },
  // 提醒面板 → 通用提醒 (#932, 2026-10-15): one switch per reminder kind over every channel (对话、桌面通知、语音、锁屏、提醒中心).
  { match: control => control.role === "switch" && /^(止损止盈触发|保护单撤销|挂单未成交|策略出卡与自动下单|成交与撤单|到价提醒|收盘前持仓|风险度|保本提醒|交割到期|盘中异动|每日复盘|今日作战表|研讨完成|AI 简讯)$/.test(control.label),
    via: "preference: notify.stopTriggered / protectionCancelled / orderUnfilled / strategySignalAlerts / desktop.fills + desktop.cancels（一个开关写两项）/ desktop.priceAlerts / marketCloseReminder / riskReminder / breakEven / delivery / movement / dailyReview / morningDesk / researchDebate / desktop.briefs（急停总是提醒，没有开关）" },
  { match: control => control.role === "select" && control.label === "保本提醒门槛", via: "preference: notify.breakEvenRule（{mode: multiple|amount, multiple, amount, amountUsd}，下拉是常用档，其他值照原样列出）" },
  { match: /^外盘（美元）(减少|增加)?$/, via: "preference: notify.breakEvenRule.amountUsd（美元整数，null = 外盘按倍数；只在金额门槛时显示）" },
  { match: /^(锁屏显示金额和手数|显示金额和手数)$/, via: "preference: notify.lockScreenAmounts（关掉后锁屏只写不带数字的短句）" },
  { match: /^提示音$/, via: "preference: notify.sound" },
  { match: /^策略信号提醒$/, via: "preference: notify.strategySignalAlerts（监控中策略信号变化的主动提醒开关）" },
  { match: /^(说完判停|口头确认时限|KD 周期|KD 平滑 K|KD 平滑 D|MACD 快线|MACD 慢线|MACD 信号|标注自动清除|默认手数|会话保留天数)(减少|增加)$/,
    via: "preference 步进器 ±：voice.endpointStallSeconds / confirmWindowSeconds, chart.kdParams.*, chart.macdParams.*, chart.aiAnnotationsAutoClearMinutes, trade.defaultVolume, data.sessionRetentionDays" },
  // 图表指标可配置 (2026-10-02): the chart labels open in-place popovers (disclosures), and the same
  // editor components sit on 设置 → 图表 → 指标参数与均线.
  { match: /^均线( \d+( · \d+)*)?( · 布林 \d+(\.\d+)?,\d+(\.\d+)?)?( · SAR [\d.]+,[\d.]+)?$/, via: "disclosure: chart-ma-editor（价格行标签按钮，就地打开均线与布林带编辑；全部关闭时显示 均线未启用）" },
  { match: control => control.group !== "chart:context-menu" && /^(KD \d+,\d+,\d+|MACD \d+,\d+,\d+|RSI \d+|CCI \d+|持仓量|成交量)$/.test(control.label), via: "disclosure: {name: chart-pane-editor, id: 副图类型}（该格标签按钮，就地打开副图编辑）" },
  { match: /^添加副图$/, via: "disclosure: chart-pane-add（最后一行标签旁的 ＋ 副图，满 3 格时不渲染）" },
  { match: control => control.role === "switch" && /^显示第 \d+ 条均线$/.test(control.label), via: "preference: chart.maLines[].enabled（该条均线的显示开关）" },
  { match: control => control.role === "switch" && control.label === "布林带", via: "preference: chart.boll.enabled" },
  { match: control => control.role === "switch" && control.label === "抛物线 SAR", via: "preference: chart.sar.enabled（2026-10-09）" },
  { match: /^(第 \d+ 条均线周期|布林周期|布林宽度|RSI 周期|CCI 周期|SAR 步长|SAR 上限)(减少|增加)$/,
    via: "preference 步进器 ±：chart.maLines[].period（2–500）、chart.boll.period / width、chart.rsiParams.period、chart.cciParams.period、chart.sar.step / max" },
  { match: control => control.role === "button" && (/^删除$/.test(control.label) || /^＋ 加一条均线(（最多 \d+ 条）)?$/.test(control.label)) && /^(chart-|settings-chart|mobile-subpanes)/.test(control.surface),
    via: "preference: chart.maLines（删除这一条 / 追加一条 120 均线；1–8 条，至少保留一条）" },
  { match: control => control.role === "select" && /^第 \d+ 格指标$/.test(control.label), via: "preference: chart.subPaneList（该格换成所选指标；别格在用的类型禁用并注明所在格）" },
  { match: control => control.role === "button" && control.label === "移除这一格" && /^(chart-|settings-chart)/.test(control.surface), via: "preference: chart.subPaneList（移除该格副图）" },
  { match: /^(KD|MACD|RSI|持仓量|CCI|成交量)副图(（已在第 \d+ 格）)?$/, via: "preference: chart.subPaneList（追加该类型副图，最多 3 格；已在别格的禁用并注明位置）" },
  { match: control => control.role === "button" && control.label === "关闭" && /^(chart-(ma-editor|pane-editor|pane-add|drawing-tools|periods-more)|board-columns|price-alerts|contract-info)$/.test(control.surface), via: "disclosure: 对应编辑卡 {open: false}" },
  { match: /^全局急停$/, via: "dialog: emergency-stop" },
  { match: /^AI 已急停 · 恢复$/, via: "dialog: resume-execution（顶栏恢复按钮，急停后出现）" },
  { match: /^恢复默认布局$/, via: "presentation: {reset: true}" },
  { match: control => control.surface.startsWith("settings-") && /(K 线音柱|灵动双眼|像素小牛)/.test(control.label), via: "preference: ui.aiAvatar = kline|eyes|pixel（设置 → 界面 → AI 形象，选项文字带头像字符前缀）" },
  { match: /^底栏自动隐藏$/, via: "preference: ui.autoHidePositionsBar（没有持仓和挂单时收起的底栏不渲染，快照 positionsBarAutoHidden）" },
  { match: /^打开新手引导$/, via: "dialog: {name: help, topic: tour}（设置 → 关于 → 版本与更新）" },
  { match: /^开始引导（\d+ 步）$/, via: "tour: {op: start, steps: help.tour}" },
  { match: /^重新高亮当前步骤$/, via: "tour 指示器：highlight 当前步骤 target（点别处只关气泡）" },
  { match: /^(上一步|下一步|完成引导|结束引导)$/, via: "tour: {op: prev|next|stop}" },
  // Conversation cards (a real fake-agent turn in the audit): every card's folded 详情.
  { match: control => control.surface === "ai-chat-card" && control.role === "summary", via: "disclosure: {name: card-details|progress-steps|advice-sources|tool-card|diff-card, id?}" },
  { match: /^清空显示$/, via: "disclosure: activity-clear" },
  { match: /^清除候选价$/, via: "selectedPrice: null（图上候选价胶囊的 ×）" },
  { match: /^清空图上标记$/, via: "chartMarks: \"clear\"（图表右键；AI 标注、回测买卖点与成交标记一并清除）" },
  // 下单面板 · 五档 (2026-10-03; the 价格阶梯 panel merged into it 2026-10-07): the group is its section's agent target.
  { match: control => control.group === "order-depth" && /^(卖|买)[一二三四五]|^最新/.test(control.label), via: "selectedPrice（下单面板任意有效档位填入价格并切限价；无报价档位隐藏，state.ticketPrice / ticketPriceMode 回报实际输入）" },
  { match: control => control.group === "order-depth" && control.label === "五档", via: "disclosure: order-depth（下单面板五档折叠，默认展开；快照 disclosures.order-depth）" },
  // 快捷技能 (2026-09-24): chip labels come from the skill registry, so match by where they sit.
  { match: control => control.group === "quick-skills", via: "chat:send（快捷技能 chip：以用户身份发该技能 quickAction 的 prompt；快照 controls 带 disabled 与 title = 原因行或“发送：<文本>”）" },
  { match: control => control.surface.startsWith("settings-") && control.group === "快捷技能" && control.role === "switch", via: "preference: ui.quickSkills（快照 preferences.ui.quickSkills 与开关 checked）" },
  { match: /^设置手续费$/, via: "settingsPage: trade-advanced（费率行；旧值 fees 映射到该二级页）" },
  // 使用说明卡 (2026-09-25 两层重做): topic buttons, 开发者参考, 常见问题, 完整说明, the search box and the narrow drop-down.
  { match: control => control.surface === "dialog-help" && control.role === "button" && !/^关闭/.test(control.label),
    via: "help: {topic | group | faq: {index, open} | detail}（旧的 dialog: {name: help, topic} 仍可用；快照 state.help）" },
  { match: control => control.surface === "dialog-help" && /^(搜索说明|选择主题)$/.test(control.label), via: "help: {query} / help: {topic}（窄卡片的主题下拉）" },
  { match: control => control.surface.startsWith("dialog-contract") && /[A-Z]+\.[A-Za-z0-9]+/.test(control.label), via: "symbol（抽屉里的最近合约与月份行）" },
  // 报价总表 (2026-10-01): the view, the exchange chips, the sortable columns and the row click-through.
  { match: /^报价$/, via: "view: board" },
  { match: control => control.surface.startsWith("board") && control.role === "button" && /^(自选|全部|上期所|能源中心|大商所|郑商所|中金所|广期所|外盘)$/.test(control.label),
    via: "boardExchange: all|watchlist|SHFE|INE|DCE|CZCE|CFFEX|GFEX|FOREIGN（总表筛选，自选＝只看星标品种，外盘＝海外主连只读报价；快照 state.boardExchange、state.boardWatchlistCount 是自选数）" },
  // 自选品种 (2026-10-02): one account list behind all three stars (报价表行 / 合约抽屉品种行 / 合约头部).
  { match: /^(加入|移出)自选 /, via: "preference: watchlist.products（“交易所.品种”整段清单：先读再增删该品种后整段写回，三处星标同一份）" },
  { match: control => control.surface.startsWith("board") && control.role === "button" && /^(涨跌幅|成交量|持仓量|增仓)排序$/.test(control.label),
    via: "boardSort: {key: changePercent|volume|openInterest|openInterestChange, order}（快照 state.boardSort）" },
  { match: control => control.surface.startsWith("board") && control.role === "button" && /[A-Z]+\.[A-Za-z0-9]+/.test(control.label),
    via: "symbol（总表行：切到该合约的交易台）" },
  // 报价总表列可自定义 (2026-10-02): the 列 popover and the narrow fold hint open the same disclosure;
  // the editor's buttons save the whole column list (设置 → 界面 → 报价总表 is the same editor).
  { match: control => control.surface.startsWith("board") && control.role === "button" && (control.label === "列" || /^\+\d+ 列已收起$/.test(control.label)),
    via: "disclosure: board-columns（总表顶部的“列”浮层 / 窄表头的收起提示，快照 disclosures.board-columns）" },
  { match: /^(上移|下移)(最新|涨跌|涨跌幅|成交量|持仓量|增仓|最高|最低|昨结|涨停|跌停|更新时间)$/,
    via: "preference: marketBoard.columns（列序整体保存，勾选框即同一列表项；设置 → 界面 → 报价总表是同一份编辑）" },
  { match: control => control.role === "button" && control.label === "恢复默认" && /^(board-columns|settings-ui)/.test(control.surface),
    via: "preference: marketBoard.columns = 默认八列" },
  // 合约抽屉两级 (2026-10-01): the product face selects its main contract, “N 个月份” expands its months.
  { match: /^\d+ 个月份$/, via: "contractProduct: \"<交易所>.<品种>\"（需抽屉已打开，null 收起；快照 state.contractProduct）" },
  // 到价提醒 (2026-10-02): the phone's 设提醒 popover, its rows and the chart's right-click create (the computer's
  // toolbar bell is the 提醒中心 since 2026-10-15, above).
  { match: /^其他合约 \d+ 条 [▾▸]$/, via: "disclosure: price-alerts-others（需 price-alerts 浮层已打开，关浮层即注销；快照 disclosures.price-alerts-others）" },
  { match: control => control.surface.startsWith("price-alerts") && control.role === "button" && / [A-Z]+\.[A-Za-z0-9]+$/.test(control.label),
    via: "symbol（“其他合约”行的合约名按钮，切到该合约的交易台）" },
  // 下单顺带保护 (2026-10-02): a position row's inline 保护 form (the fake account holds au2612/ag2612
  // longs, so the rows render; the form itself opens by the disclosure below and is audited there).
  { match: /^给 [A-Z]+\.[A-Za-z0-9]+ (多|空)仓挂保护$/, via: "disclosure: {name: position-protection, id: position:<symbol>:<direction>}（持仓行的保护表单；开仓卡的保护输入在 ticket 里随单走）" },
  
  // 合约信息卡 (2026-10-02): the contract head's ⓘ and delivery badge open the same card.
  { match: /^合约信息$/, via: "disclosure: contract-info（合约头部的 ⓘ，快照 disclosures.contract-info）" },
  { match: /^交割月提醒：/, via: "disclosure: contract-info（合约头部的交割提醒标，同一张卡；快照 disclosures.contract-info）" },

  // 画线第二版 (2026-10-09): the chart's drawing toolbar (group chart:drawing-toolbar), shown only while drawing
  // (owner 2026-10-14). A tool only arms a placement — the chart clicks that follow create the drawing, which is the
  // capability below. Snapshot: state.chartDrawings.
  { match: control => control.group === "chart:drawing-toolbar" && /（(线条|价位|分割|形状|标注|测量)）$/.test(control.label),
    via: "disclosure: {name: chart-drawing-tool, id: <工具名>, open: true}（组按钮 = 这组最后用过的工具；快照 state.chartDrawings.tool）" },
  { match: control => control.group === "chart:drawing-toolbar" && /：更多工具$/.test(control.label), via: "disclosure: {name: chart-drawing-group, id: lines|levels|fib|shapes|notes|measure}" },
  { match: control => control.group === "chart:drawing-toolbar" && control.role === "menuitemradio", via: "disclosure: {name: chart-drawing-tool, id: <工具名>, open: true}（组内工具列表）" },
  { match: control => control.group === "chart:drawing-toolbar" && control.label === "磁吸（贴到开高低收）", via: "disclosure: chart-drawing-magnet（= preference chart.drawingMagnet）" },
  { match: control => control.group === "chart:drawing-toolbar" && control.label === "全部锁定", via: "disclosure: chart-drawing-lock-all（= preference chart.drawingsLocked）" },
  { match: control => control.group === "chart:drawing-toolbar" && control.label === "全部隐藏", via: "disclosure: chart-drawings-hidden（= preference chart.drawingsHidden）" },
  { match: control => control.group === "chart:drawing-toolbar" && /^全部删除（本合约 \d+ 条）$/.test(control.label), via: "disclosure: chart-drawing-clear（打开二次确认）" },
  { match: control => control.group === "chart:drawing-toolbar" && control.label === "收起画线工具栏", via: "disclosure: {name: chart-drawing-toolbar, open: false}（收起并回十字光标）" },
  // The selected drawing's floating style bar (group chart:drawing-style) and its 精确设置 (group chart:drawing-settings).
  { match: control => control.group === "chart:drawing-style" && control.label === "精确设置", via: "disclosure: {name: chart-drawing-settings, id: <线 id>}" },
  { match: control => control.group === "chart:drawing-settings" && control.label === "关闭", via: "disclosure: {name: chart-drawing-settings, id: <线 id>, open: false}" },
  // The chart's right-click menus (group chart:context-menu) and their submenus.
  { match: control => control.group === "chart:context-menu" && control.label === "画线", via: "disclosure: {name: chart-context-menu, id: main/draw}" },
  { match: control => control.group === "chart:context-menu" && /^周期/.test(control.label), via: "disclosure: {name: chart-context-menu, id: main/period}" },
  { match: control => control.group === "chart:context-menu" && /^在这里设提醒/.test(control.label), via: "disclosure: {name: chart-context-menu, id: main/alert}" },
  { match: control => control.group === "chart:context-menu" && /^到价提醒/.test(control.label), via: "POST /api/market/alerts（图表右键 · 在这里设提醒，价取自点击处并已按 tick 对齐）" },
  { match: control => control.group === "chart:context-menu" && /^(按打法：.+|按条件新建提醒…)$/.test(control.label), via: "strategyEditor: {new: true, reminder: true, ask?: 打法的进场规则}（提醒模式）" },
  { match: control => control.group === "chart:context-menu" && /^主图指标/.test(control.label), via: "disclosure: {name: chart-context-menu, id: main/indicator}" },
  { match: control => control.group === "chart:context-menu" && /^添加副图/.test(control.label), via: "disclosure: {name: chart-context-menu, id: main/add-pane | pane:<type>/add-pane}" },
  { match: control => control.group === "chart:context-menu" && /^换成/.test(control.label), via: "disclosure: {name: chart-context-menu, id: pane:<type>/swap}" },
  { match: control => control.group === "chart:context-menu" && control.label === "颜色 / 粗细 / 线型", via: "disclosure: {name: chart-context-menu, id: drawing:<线 id>/style}" },
  { match: control => control.group === "chart:context-menu" && control.label === "精确设置…", via: "disclosure: {name: chart-drawing-settings, id: <线 id>}" },
  { match: control => control.group === "chart:context-menu" && control.label === "测量区间", via: "disclosure: {name: chart-drawing-tool, id: measure, open: true}" },
  { match: control => control.group === "chart:context-menu" && /^(趋势线|水平线|射线|垂直线|通道|区间|黄金分割|文字)$/.test(control.label),
    via: "disclosure: {name: chart-drawing-tool, id: <工具名>, open: true}（右键 画线 ▸ 的八个常用工具）" },
  { match: control => control.group === "chart:context-menu" && /^(线条|价位|分割|形状|标注|测量)$/.test(control.label), via: "disclosure: {name: chart-drawing-group, id: lines|levels|fib|shapes|notes|measure}（工具栏随组列表出现）" },
  { match: control => control.group === "chart:context-menu" && /^(分时|1 分|3 分|5 分|15 分|30 分|1 小时|日线|周线)$/.test(control.label), via: "duration / chartView: timeshare（右键 周期 ▸）" },
  { match: control => control.group === "chart:context-menu" && /^(均线|布林带)$/.test(control.label), via: "preference: chart.maLines[].enabled / chart.boll.enabled（右键 主图指标 ▸）" },
  { match: control => control.group === "chart:context-menu" && control.label === "均线与布林带设置…", via: "disclosure: chart-ma-editor" },
  { match: control => control.group === "chart:context-menu" && control.label === "参数设置…", via: "disclosure: {name: chart-pane-editor, id: <副图>}" },
  { match: control => control.group === "chart:context-menu" && /^(KD|MACD|RSI|持仓量|CCI|成交量|上移|下移|关闭这个副图)$/.test(control.label), via: "preference: chart.subPaneList（换成 / 添加 / 上移下移 / 关闭副图）" },
  { match: control => control.group === "chart:context-menu" && control.label === "图表设置…", via: "settingsPage: chart" },
  { match: control => control.group === "chart:context-menu" && control.label === "磁吸", via: "disclosure: chart-drawing-magnet（= preference chart.drawingMagnet）" },
  { match: control => control.group === "chart:context-menu" && control.label === "全部锁定", via: "disclosure: chart-drawing-lock-all（= preference chart.drawingsLocked）" },
  { match: control => control.group === "chart:context-menu" && control.label === "全部隐藏", via: "disclosure: chart-drawings-hidden（= preference chart.drawingsHidden）" },
  { match: control => control.group === "chart:context-menu" && /^全部删除（\d+ 条）…$/.test(control.label), via: "disclosure: chart-drawing-clear（打开二次确认）" },
  { match: control => control.group === "chart:context-menu" && /^让 AI (分析这个价|讲讲这一段)$/.test(control.label), via: "chat:send（以用户身份发这句问题，不建单）" },

  { match: control => control.role === "summary" && /^手续费$/.test(control.label), via: "disclosure: replay-fees" },
  { match: /^(取消|暂不恢复|稍后)$/, via: "dialog: {open: false}（更新窗的 稍后 = 本次运行内该版本不再弹）" },
  { match: /^切文字$/, via: "panel: ai（voice-menu）" },
  { match: /^AI 动态$/, via: "aiActivityCard（voice-menu）" },
  { match: /^收起 AI 动态$/, via: "aiActivityCard: closed" },
  { match: /^让 AI (分析此价|讲讲这段)$/, via: "chat:send（把价位/可见区间写进 text）" },
  // 结构化卡片 (2026-10-04): 追问卡的选项、卡片与快讯卡的「问 AI」都以用户消息发出预填文字；
  // 界面按钮只放行 panel/news/positionsTab/ticketMode/strategyTab/symbol/view，按钮 title 写着实际命令。
  { match: /^问 AI/, via: "chat:send（卡片按钮：以用户消息发出预填问题/选项文字，AI 直接发同一句；按钮 title = “发送：<文本>”）" },
  { match: /^回答：/, via: "chat:send（卡片按钮：以用户消息发出预填问题/选项文字，AI 直接发同一句；按钮 title = “发送：<文本>”）" },
  { match: /^打开调研$/, via: "news: {tab: desk}（判断卡）" },
  { match: /^打开下单台$/, via: "panel: order（没能生成方案卡）" },
  { match: /^看图表$/, via: "symbol + view: trade（到价提醒卡 / 回答卡 / 走势数据卡）" },
  { match: /^打开委托$/, via: "positionsTab: orders（挂单未成交提醒卡 / 挂单与保护数据卡）" },
  { match: /^打开持仓$/, via: "panel: positions（风险度 / 交割提醒卡 / 持仓数据卡）" },
  // 回答卡片化 (2026-10-14): 回答卡 / 数据卡's folded 依据 and the 快讯数据卡's way to the news panel.
  { match: control => control.role === "summary" && control.label === "依据", via: "disclosure: {name: card-details, id: 卡id}（回答卡 / 数据卡底部的依据：读取时间与来源）" },
  { match: /^打开新闻$/, via: "news: {tab: flash|news}（快讯 / 消息面数据卡；手机打开调研）" },
  // 手机版 (2026-10-04): the phone layout's bottom tabs, the AI 动态 price chip and the chat page's back arrow.
  { match: control => control.group === "mobile-tabs", via: "mobileTab: ai|research|market|strategy|me（手机版底部标签；旧值 notify = ai；AI 标签下的对话页是 mobileTab: chat = panel: ai；快照 state.mobileLayout、state.mobileTab）" },
  { match: /^看盘：/, via: "mobileTab: market（手机版 AI 首页顶部的合约价格胶囊）" },
  { match: /^返回 AI 首页$/, via: "mobileTab: ai（手机版对话页左上角返回）" },
  // 10-05: AI 首页's bottom 打字问 AI field (opens the chat with its input focused).
  { match: /^打字问 AI$/, via: "mobileTab: chat（手机版 AI 首页底部“打字问 AI”输入框；agent 要说话直接 chat:send）" },
];

/** Controls the catalog covers with a business capability instead of a UI command. */
export const CAPABILITY: Rule[] = [
  // 多 AI 研讨 (2026-10-11): 写进今日判断 and 取消 are business calls on the debate.
  { match: control => control.group === "research-debate" && /^(写进今日判断|正在写入…|已写进今日判断（.+）)$/.test(control.label),
    via: "POST /api/research/debates/{id}/adopt（研讨结论经调研员的核对写进今日判断，进历史；快照 state.researchDebate.canAdopt / adopted）" },
  { match: control => ["research-debate", "panel:debate"].includes(control.group ?? "") && control.label === "停止", via: "POST /api/research/debates/{id}/cancel（停止进行中的辩论；快照 state.researchDebate.status）" },
  { match: control => control.label === "确认删除" && control.surface === "ai-strategy-delete-confirm", via: "DELETE /api/strategy-plans/{strategy_id}（仅在用户明确要求删除时调用）" },
  // 今日复盘 (2026-10-06): 重新生成 and the 习惯 observation's answers are business calls (AI: regenerate_daily_review /
  // answer_review_observation — 记住 only on the user's own words, with the quote).
  { match: /^(重新生成|生成中)$/, via: "POST /api/account/daily-review/regenerate {tradingDay}（重算并覆盖，不推送；快照 review.dailyReview）" },
  { match: control => /review/.test(control.surface) && /^(记住|不对)$/.test(control.label),
    via: "POST /api/account/daily-review/observation {tradingDay, verdict: remember|dismiss, quote?}（AI 代记住必须带用户原话；快照 review.dailyReview.observation）" },
  // 我的流程 (2026-10-06): 改 hands the chat “改一下『名称』：” (the AI answers with a new 流程卡), the 流程卡's 改一下
  // fills the same sentence; 确认删除 is the delete itself.
  { match: /^改「.+」$/, via: "POST /api/routines/drafts {routineId, name, steps}（改流程 = AI 出新的流程卡、用户点保存；设置里的“改”打开对话并预填“改一下『名称』：”）" },
  { match: control => control.surface.startsWith("ai-chat-routine") && control.label === "改一下",
    via: "POST /api/routines/drafts（改一下：输入框预填“改一下『名称』：”，用户说怎么改，AI 出新的流程卡）" },
  { match: /^确认删除「.+」$/, via: "DELETE /api/routines/{routine_id}（AI 只在用户明确说删时调用；列表 GET /api/routines）" },
  { match: /^删除未关联的补充：/, via: "DELETE /api/account/trade-notes/{round_id}（传 unlinkedNotes[].roundId 原键；列表在 GET /api/account/pnl 与 /api/trade-review）" },
  { match: /^重新检查更新$/, via: "受保护核心：软件更新走原更新流程（不向 agent 开放）" },
  { match: /^(立即更新|重试下载|下载并安装|正在下载更新…|正在安装更新…)( \S+)?$/, via: "受保护核心：更新的下载/安装由用户点击（更新窗本身可用 dialog: {name: update-dialog} 开关）" },
  { match: /^暂停下载$/, via: "受保护核心：更新下载的暂停/继续由用户操作（进度同时显示在更新窗与设置 → 更新状态）" },
  // 网页新版本 (2026-10-05): the web build's update is a page reload — the user's click, or the page's
  // own reload when it comes back to the foreground idle; state in snapshot context.webUpdate.
  { match: /^(检查更新|检查中…)$/, via: "受保护核心：网页版检查更新 = 读已部署 version.json，有新版即重新加载页面（设置 → 关于、手机我的底部；快照 context.webUpdate.available）" },
  { match: control => control.group === "web-update-banner" && /^(刷新|关闭新版本提示)$/.test(control.label),
    via: "受保护核心：新版本已就绪提示条，刷新 = 重新加载页面，关闭只藏提示条（空闲回到前台仍自动刷新；快照 context.webUpdate.banner）" },
  { match: /^语音音色$/, via: "preference: voice.voice（下拉，选项来自 GET /api/voice/realtime/voices；同 PUT /api/voice/realtime/voice）" },
  { match: /^(对话 JSON|对话 Markdown)$/, via: "导出 = 本机下载；agent 读对话走 GET /api/chat-sessions 与 GET /api/sessions/{id}" },
  { match: /^(复盘 JSON|复盘 Markdown)$/, via: "导出 = 本机下载；agent 读复盘走 GET /api/trade-review" },
  { match: /^(生成简讯|AI 简报推送)$/, via: "PUT /api/app/config {showMarketBriefs}（提醒面板 → 通用提醒 · AI 简讯的订阅品种下；手机版我的 → AI 简报推送页顶部是同一开关）" },
  // 提醒中心 (#932, 2026-10-15): read marks, earlier days and 保本's answer through their business routes; a row goes to its target.
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && control.label === "全部已读", via: "POST /api/reminders/read {all: true}（已读记在服务端，各设备一起变）" },
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && control.label === "查看更早", via: "GET /api/reminders?scope=earlier&before=<cursor>（按交易日往前翻，服务端留 30 天）" },
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && /^(移到成本|不用)$/.test(control.label),
    via: "POST /api/ai-execution/breakeven/{id}/{move|decline}（保本提醒的答复，与对话里那张卡同一接口，不下单）" },
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && /^(添加|设置中…)$/.test(control.label),
    via: "POST /api/market/alerts（新建表单；AI 用 create_price_alert 工具；direction 由 Runtime 按最新价判定）" },
  { match: control => REMINDER_GROUPS.has(control.group ?? "") && control.role === "button" && !/^(删除提醒：|价格(减|加)一跳)/.test(control.label),
    via: "POST /api/reminders/read {ids} + 这条的 target：chatSession + 对话里那张卡 / positionsTab: orders / reviewDay / mobileTab: research 或 news: {tab: desk|flash}（提醒中心的一行）" },
  { match: control => (control.group === "订阅品种" && /^(上移|下移|取消订阅)/.test(control.label)) || (control.group === "添加品种" && /^添加/.test(control.label)),
    via: "PUT /api/app/brief-subscriptions {products}（整表替换，顺序即推送顺序；GET 同路径读当前列表、来源与档位上限）" },
  { match: /^本金$/, via: "复盘本金：GET/PUT /api/app/preferences" },
  // 保本提醒卡 (2026-10-06): its two answers are the question's own business route (the user's click).
  { match: control => control.role === "button" && /^(移到成本|不用)$/.test(control.label),
    via: "POST /api/ai-execution/breakeven/{id}/move | decline（GET /api/ai-execution/breakeven 列出问题与结果；移到成本 = 止损改到成本、止盈不动）" },
  // 锁屏提醒 (2026-10-06): the device list's own REST surface (subscribing itself is the user's gesture).
  { match: control => (control.surface === "mobile-push" || control.surface.startsWith("reminders-general")) && /^(发送|发一条试试)$/.test(control.label),
    via: "POST /api/app/push/test {deviceId?}（发一条试试：本人所有设备或指定一台，返回每台 sent / removed / failed）" },
  { match: control => (control.surface === "mobile-push" || control.surface.startsWith("reminders-general")) && /^(移除( .+)?|关闭锁屏提醒)$/.test(control.label),
    via: "DELETE /api/app/push/subscription?deviceId=（移除一台设备的锁屏提醒；本机关闭同一接口）" },
  // 设置 → 记忆 / 二级页「约定、经验与最近变动」(2026-09-30): the memory document's own REST surface.
  { match: control => control.surface.startsWith("settings-memory") && /^(添加|保存|取消|修改|删除|撤销|现在整理一次)$/.test(control.label),
    via: "记忆文档：POST /api/memory、PUT|DELETE /api/memory/{id}、POST /api/memory/changes/{id}/undo、POST /api/memory/consolidate" },
  { match: /^记忆层级$/, via: "POST /api/memory body 的 layer（「添加一条」的下拉）" },
  { match: /^保存$/, via: "POST /api/ai-execution/limits" },
  { match: /^平台上限/, via: "POST /api/ai-execution/limits {enabled}" },
  { match: /^手续费 交易所标准（默认）/, via: "GET/POST /api/trading-costs（settingsPage: trade 打开编辑）" },
  { match: /^(本金|单笔最多手数|累计最多委托)(减少|增加)$/, via: "步进器 ±：值同该行（本金 → /api/app/preferences；限额 → /api/ai-execution/limits）" },
  { match: /^切换模型$/, via: "PUT /api/agent/models/preference（模型目录）" },
  { match: /^(登录|注册|注册并登录|退出账户)$/, via: "云端账号：登录/注册/退出走认证流程（受保护核心，不向 agent 开放）" },
  { match: /^(开通本站模拟|正在开通…)$/, via: "POST /api/accounts/open-paper（设置 → 账户 → 交易账户行，没有本站模拟账户时才有；开户是用户自己点的动作，不进能力目录）" },
  { match: /^示例趋势策略回测$/, via: "POST /api/backtests/tqsdk / tool:run_tqsdk_historical_backtest（内置示例趋势策略，不是用户的策略）" },
  { match: /^上线运行$/, via: "dialog: {name: strategy-launch, open: true}（上线对话框：合约与执行方式）" },
  { match: /^刷新账户$/, via: "GET /api/account（读取即刷新）" },
  { match: /^(自己交易|AI 自动策略|开始手动回放)$/, via: "POST /api/replays（mode 与合约在 body）" },
  { match: /^确认全局急停$/, via: "POST /api/ai-execution/stop（受保护：风险权限，agent 用 stop_simulation_execution 工具）" },
  { match: /^只停新开仓$/, via: "POST /api/ai-execution/stop {scope: opens}（可逆的轻量停机；受保护风险操作，agent 用 stop_simulation_execution 工具）" },
  { match: /^确认恢复模拟执行$/, via: "POST /api/ai-execution/resume（受保护：用户明确恢复）" },
  { match: /^(买多|卖空|限价|对手价|最新价|确认下单|一键买多.*|一键卖空.*|一键平多.*|一键平空.*|一键平仓.*|智能平仓.*|平今.*|平昨.*|挂单|生成方案卡|已是目标持仓)$/,
    via: "交易意图：委托走 POST /api/simulation/orders / propose_order，界面不提供下单命令（能力表“需要交易意图”类）" },
  // 意图重构第 4 步 (2026-10-16): the 平仓页's 自动 beside 平今 / 平昨 = no positionBucket (the planner allocates).
  { match: control => control.surface.includes("order") && control.label === "自动",
    via: "交易意图：平今平昨在 POST /api/simulation/proposals body 的 positionBucket 里（不带 = 自动，Runtime 按偏好分配）" },
  { match: /^委托价(减|加)一跳$/, via: "交易意图：委托价在 /api/simulation/orders body 里" },
  // 手机版下单页: the side buttons carry the opponent price; the confirm names the order (same submit path).
  { match: /^(买多卖一|卖空买一) /, via: "交易意图：方向在 /api/simulation/orders / propose_order body 里（手机版按钮附对手价）" },
  { match: /^确认(买多|卖空) \d+ 手 @ /, via: "交易意图：手机版下单页的确认 = 电脑“确认下单”，同一确认后下单接口" },
  { match: /^(手数|目标净持仓)(减一|加一)$/, via: "交易意图：手数/目标净持仓在 /api/simulation/orders / target-position body 里（aria-label 已补，快照可见）" },
  { match: /^发送$/, via: "chat:send（对话输入框）" },
  // 到价提醒 (2026-10-02): the popover's form and rows, and the chart's right-click create. The price
  // itself never became a UI command by design — the Runtime judges the direction against the live quote.
  { match: control => control.surface.startsWith("price-alerts") && /^(添加|设置中…)$/.test(control.label),
    via: "POST /api/market/alerts（AI 用 create_price_alert 工具；direction 由 Runtime 按最新价判定）" },
  { match: /^价格(减|加)一跳$/, via: "POST /api/market/alerts body 的 price（浮层表单按 priceTick 步进）" },
  { match: /^删除提醒：.+$/, via: "DELETE /api/market/alerts/{alert_id}（AI 用 cancel_price_alert 工具）" },
  // 用户画线 (2026-10-02; 画线第二版 2026-10-09): placing, moving, restyling and deleting walk the same catalog routes
  // the AI's chart-drawings skill tools use (create / update / delete / clear_chart_drawings).
  { match: control => control.group === "chart:drawing-toolbar" && /^(取消|删除 \d+ 条)$/.test(control.label),
    via: "DELETE /api/market/drawings?symbol=（全部删除的二次确认；只删本合约自己画的线，AI 用 clear_chart_drawings）" },
  { match: control => control.group === "chart:drawing-style" && /^(颜色：|粗细 [\d.]+$|实线$|虚线$|点线$|向两端延长$|锁定$|解锁$)/.test(control.label),
    via: "PUT /api/market/drawings/{drawing_id} {style}（AI 用 update_chart_drawing）" },
  { match: control => control.group === "chart:context-menu" && (/^(颜色：|粗细 [\d.]+$)/.test(control.label) || /^(实线|虚线|点线)$/.test(control.label)),
    via: "PUT /api/market/drawings/{drawing_id} {style}（右键 颜色 / 粗细 / 线型 ▸；AI 用 update_chart_drawing）" },
  { match: control => control.group === "chart:context-menu" && /^(锁定|解锁)$/.test(control.label), via: "PUT /api/market/drawings/{drawing_id} {style: {locked}}" },
  { match: control => control.group === "chart:context-menu" && control.label === "复制一条", via: "POST /api/market/drawings（同样的线错开五跳；AI 用 create_chart_drawing）" },
  { match: control => control.group === "chart:context-menu" && /^显示单独隐藏的 \d+ 条线$/.test(control.label), via: "PUT /api/market/drawings/{drawing_id} {style: {hidden: null}}" },
  { match: control => control.group === "chart:drawing-settings" && (control.role === "switch" || /^(取消|确定|保存中…)$/.test(control.label)),
    via: "PUT /api/market/drawings/{drawing_id} {points, label, style}（精确设置；存为默认样式 = preference chart.drawingDefaults）" },
  { match: control => control.group === "chart:drawing-hint" && control.label === "保留", via: "POST /api/market/drawings {kind: measure}（测量结果默认不保存，保留才存）" },
  { match: control => control.group === "chart:drawing-hint" && control.label === "清除", via: "disclosure: {name: chart-drawing-tool, id: measure, open: false}（清掉这次测量；快照 state.chartDrawings.measuring）" },
  { match: /^(删除这条线|设到价提醒)$/, via: "DELETE /api/market/drawings/{drawing_id} / POST /api/market/alerts（样式条与右键；选中后 Delete 键同一能力）" },
  { match: /^在这条线设到价提醒$/, via: "POST /api/market/alerts（价取这条线在最新 K 线处的价位，按 tick 对齐；direction 由 Runtime 按最新价判定）" },
  // 下单顺带保护 (2026-10-02): the ticket's 止损/止盈 steppers ride the order body's protection; the
  // position form's 提交/撤销/桶 chips are POST /api/ai-execution/stops (+ /{id}/cancel) — the same
  // capability the AI reaches with set_simulation_stop_loss.
  { match: /^(止损价|止盈价)(减|加)一跳$/, via: "交易意图：开仓卡 protection 的止损/止盈价在 propose_order body 里（步进按 priceTick 对齐）" },
  { match: control => /^(ai-positions|positions-bar|position-protection)/.test(control.surface) && control.label === "提交",
    via: "POST /api/ai-execution/stops（持仓行保护表单：止损/止盈至少一个，direction 由 Runtime 按现价校验）" },
  { match: control => /^(ai-positions|positions-bar|position-protection)/.test(control.surface) && control.label === "撤销",
    via: "POST /api/ai-execution/stops/{stop_id}/cancel（撤销自己挂的手动保护；策略保护行只读）" },
  { match: control => /^(ai-positions|positions-bar|position-protection)/.test(control.surface) && /^(今|昨|全部) \d+$/.test(control.label),
    via: "POST /api/ai-execution/stops body 的 positionBucket / volume（保护哪部分仓量）" },
];

/** Not for agents, by design; each line says why. */
export const EXEMPT: Exemption[] = [
  { match: control => control.surface === "settings-voice" && control.role === "button" && control.label === "连接遥控器", reason: "连接硬件必须用户点击并在系统或浏览器选择设备；AI 不能代选或自动连接，状态在 state.remoteSettings" },
  { match: /^(一键(买多|卖空|平仓|平今|平一半|反手)|撤本合约挂单 .*|买多开仓 .*|卖空开仓 .*|卖出平仓 .*|买入平仓 .*)$/, reason: "仅人工交易确认控件；AI 只创建候选卡，不提供直接下单或撤单的界面命令" },
  { match: control => control.role.startsWith("input:") || control.role === "textarea" || control.role === "select" && control.label === "", reason: "自由输入框：快照省略输入值（凭据边界），值由对应接口的 body 承载" },
  { match: /^搜索/, reason: "搜索框：agent 用 GET /api/instruments?search= 或 symbol 命令" },
  { match: /^允许桌面通知$/, reason: "浏览器通知权限弹窗只能由用户手势触发；权限状态在快照 settings.notificationPermission" },
  { match: /^(打开锁屏提醒|这台 .+ 收锁屏提醒)$/,
    reason: "开启锁屏提醒必须由用户在这台设备上点、在系统询问里允许，浏览器才给推送订阅（PUT /api/app/push/subscription 不在能力目录）；状态在快照 state.pushSubscribed 与 GET /api/app/push" },
  { match: /^(录制.+|恢复默认快捷键)$/, reason: "本机快捷键由键盘录制，不进账号偏好；当前绑定在快照 settings.shortcuts" },
  { match: /^按住说话(，松开发送)?$/, reason: "按住说话是用户的物理语音输入（与快捷键同类），没有命令；状态在快照 context.voice.ptt，按住键在 settings.shortcuts.ptt" },
  { match: /^(清除本地缓存|诊断 JSON)$/, reason: "本机维护动作（清 localStorage / 下载诊断文件），不对应业务能力" },
  { match: control => control.group === "mobile-notify" && control.label === "全部已读", reason: "手机版 AI 首页时间线的已读标记只存在本机浏览器（按用户），不对应业务能力；未读数在快照 state.mobileUnread" },
  { match: control => control.group === "mobile-ai" && /^(还有 \d+ 张待确认|收起其余待确认)$/.test(control.label),
    reason: "AI 首页置顶区的折叠行只就地展开 / 收起其余待确认卡的显示（确认只在展开后的完整卡上由用户点），不改业务状态" },
  { match: control => control.group === "mobile-ai" && /^待确认/.test(control.label),
    reason: "AI 首页 · 待确认格只把页面滚到置顶的确认卡；卡就是对话里的确认卡（确认由用户点，卡片控件同对话卡），没有单独的界面状态" },
  { match: control => control.group === "mobile-lab" && control.role === "button" && !/^(选用这一组|正在复核并保存…|上线运行|正在验证并上线…|已上线|问 AI|返回策略)$/.test(control.label),
    reason: "回测结果页表格里点一组只改本页显示哪一组（曲线与下面的卡），不改业务状态；选用走 POST /api/strategy-labs/{labId}/choose，已选用的在快照 state.mobileLab.chosen" },
  { match: control => control.group === "mobile-install" && /^(复制链接|没有这个选项？复制链接|换浏览器打开|已复制)$/.test(control.label),
    reason: "安装引导的复制链接只把网址写进本机剪贴板，给用户换浏览器打开用，不对应业务能力" },
  { match: control => control.group === "mobile-install" && control.label === "安装到桌面",
    reason: "调用浏览器自己的安装框（beforeinstallprompt），只能由用户手势触发、在浏览器里确认；是否已安装见快照 state.installEnvironment" },
  // 问题与建议 (2026-10-06): the user's own feedback mail. Its form lives only on the page (nothing stored or
  // uploaded), and its two actions hand the draft to this machine's mail program or clipboard — no server capability.
  { match: control => control.surface.startsWith("settings-feedback") && (/^(遇到问题|提建议)$/.test(control.label) || control.label === "附上基础诊断"),
    reason: "反馈邮件草稿的本页表单（类别、是否附基础诊断）：只改本页草稿，不保存、不上传，没有对应业务能力；状态在快照 controls 的 checked" },
  { match: control => control.surface.startsWith("settings-feedback") && /^(打开邮件草稿|复制反馈内容)$/.test(control.label),
    reason: "用户操作保护边界：由用户点击把草稿交给本机邮件程序（固定收件人 mailto）或剪贴板，是否发送由用户在邮件里决定；软件不代发、不上传，agent 不代点" },
  { match: control => control.surface.startsWith("ai-chat-routine") && control.label === "保存",
    reason: "用户操作保护边界：流程卡的“保存”只由用户本人点（POST /api/routines {draftId}；agent 调用一律 403，AI 只出流程卡）；卡的状态在快照卡文本与 card.routine 事件" },
  { match: /^下载客户端$/, reason: "网页版侧栏设置上方的外部链接：新标签打开官网下载页，不对应业务能力（桌面版不渲染）" },
  // 快讯页签 (2026-10-03): a row's text is its own two-line fold; the 原文 link opens the source page.
  // (A translated row's fold is commandable — flash-orig — so its label stays out of this exemption.)
  { match: control => control.group === "news:flash" && control.role === "button"
      && !/^(当前品种|持仓与自选|宏观|全部|切到 .+ 主力合约|展开全部 \d+ 条|收起|按时间|按影响|看筛掉了什么|收起筛掉的|(展开|收起) \d+ 家报道|(展开|收起)次要消息|原文 (展开|收起)|研讨：.+|打开这条快讯的研讨结果)$/.test(control.label),
    reason: "快讯（宏观 / 全部原样列出时）正文的两行折叠只改显示行数（翻译过的行同时是 disclosure: flash-orig，可开合英文原文）：全文已在快照 text 与 GET /api/research/flash 的 items[].text / origTitle 里" },
  { match: control => control.group === "news:flash" && control.role === "a", reason: "快讯原文外链：新标签打开来源网页；链接在 GET /api/research/flash 的 items[].link" },
  // 调研页改版 (2026-10-07 第七批).
  { match: control => control.group === "mobile-research" && control.label === "快讯 →",
    reason: "手机调研页新闻面一行的“快讯 →”只把页面滚到下面的相关快讯（手机没有快讯页），不改界面状态；快讯内容在快照文本与 GET /api/research/flash" },
  { match: control => control.group === "research-debate" && /^(展开|收起)$/.test(control.label),
    reason: "裁判取舍一句超过两行时的展开 / 收起只改显示行数：全文已在快照文本与 GET /api/research/debates/{id} 的 turns 里" },
  { match: control => control.group === "research-debate" && control.role === "a",
    reason: "研讨资料来源的原文外链：新标签打开来源网页；链接在 GET /api/research/debates/{id} 的 facts[].url" },
  { match: control => (control.group === "panel:news" || control.group === "mobile-research") && control.role === "a",
    reason: "调研页相关报道与资料来源的原文外链：新标签打开来源网页；链接在 GET /api/research/desk 的 facts[].url" },
];

/**
 * Open gaps the audit tolerates for now — the burn-down list. Remove an entry when its control gains
 * a command or a snapshot field; the audit fails when an entry no longer matches anything.
 */
export const KNOWN_GAPS: KnownGap[] = [
  // 2026-09-18: the first run's four gaps (手数 ± aria-label, 恢复默认布局, 清空显示, 候选价) were closed the
  // same day; the strategy designer, a fake-agent proposal card and its confirmation dialog are audited
  // since step two (the card's 详情 through a real fake-agent turn). Still unreachable here: review
  // month/day expansion (needs closed trades — the fake account has none) and the proposal card with its
  // confirmation dialog (the execution Runtime's gateway refuses fixture quotes for order preparation;
  // the plain suite's "AI trade remains a candidate" test covers that dialog's user path).
];

export type Verdict = { kind: "command" | "capability" | "exempt" | "known-gap" | "gap" | "unobserved"; via: string };

export function classify(control: Control): Verdict {
  const exempt = EXEMPT.find(rule => test(rule.match, control));
  if (exempt) return { kind: "exempt", via: exempt.reason };
  const known = KNOWN_GAPS.find(gap => test(gap.match, control));
  const commandable = COMMANDABLE.find(rule => test(rule.match, control));
  const capability = commandable ? undefined : CAPABILITY.find(rule => test(rule.match, control));
  const reached = commandable ?? capability;
  if (reached) {
    // Reachable but invisible to the snapshot is still a gap, unless the gap list names it.
    if (!control.observed) return known ? { kind: "known-gap", via: known.gap } : { kind: "unobserved", via: reached.via };
    return { kind: commandable ? "command" : "capability", via: reached.via };
  }
  return known ? { kind: "known-gap", via: known.gap } : { kind: "gap", via: "" };
}

export function staleKnownGaps(controls: Control[]): KnownGap[] {
  return KNOWN_GAPS.filter(gap => !controls.some(control => test(gap.match, control)));
}
