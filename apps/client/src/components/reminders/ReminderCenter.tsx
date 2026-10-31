import { useState, type ReactNode } from "react";
import { Bell, CheckCheck, ChevronDown, ChevronLeft, ChevronRight, Plus, X } from "lucide-react";
import { chartSymbolOf } from "../../api";
import { HelpTip } from "../Blocks";
import { reminderKind } from "../notifyKinds";
import { AlertForm } from "../futures/PriceAlertsControl";
import { alertLine, type PriceAlertsHandle } from "../../hooks/usePriceAlerts";
import { useDisclosureSurface } from "../../uiSurfaces";
import { groupReminders, reminderClock, reminderState, tradingDayLabel, type ReminderFilter, type ReminderItem, type RemindersHandle } from "../../reminders";
import "./ReminderCenter.css";

const DAY_HELP = "一个交易日从前一晚 21:00 夜盘算起，到当天 15:00 收盘。当天的提醒都在这里，电脑和手机读同一份记录，已读也一起变；更早的在服务端留 30 天。点一条：交易类跳到对话里那张卡，挂单去委托，信息类打开调研或新闻。";
const ALERT_HELP = "价格到了只提醒一次。触发后从这一栏移走，记进下面当天的提醒里。点合约名切到那个合约的图。";
const DEST: Record<string, string> = { chat: "对话", orders: "委托", research: "调研", news: "新闻", review: "复盘" };

/** What the center needs from the window: the alerts handle and the contract the form defaults to. */
export interface ReminderCenterProps {
  reminders: RemindersHandle;
  alerts?: PriceAlertsHandle;
  contract?: { symbol?: string; name?: string; priceTick?: number | null; lastPrice?: number | null };
  filter: ReminderFilter;
  onFilter: (filter: ReminderFilter) => void;
  /** A row: mark it read and go where it points. */
  onOpen: (item: ReminderItem) => void;
  /** 保本提醒's 移到成本 / 不用, through the card's own route (never an order). */
  onBreakeven?: (breakevenId: string, choice: "move" | "decline") => void;
  onSwitchContract?: (symbol: string) => void;
  mobile?: boolean;
  /** The account's name from the account directory, given only while the user has more than one account: a 成交 /
   *  撤单 / 挂单 row names its account on its first line, so two accounts' like orders do not read as one (#932 F7). */
  accountName?: (accountId: string) => string | undefined;
}

function KindTag({ kind }: { kind: string }) {
  const info = reminderKind(kind);
  const Icon = info.icon;
  return <span className="rc-tag" data-tone={info.tone}><Icon aria-hidden="true" />{info.label}</span>;
}

/** The kind's icon alone, in its tone (设置 → 提醒's rows). */
export function KindIcon({ kind }: { kind: string }) {
  const info = reminderKind(kind);
  const Icon = info.icon;
  return <span className="rc-tag rc-tag-icon" data-tone={info.tone} aria-hidden="true"><Icon /></span>;
}

type RowProps = { item: ReminderItem } & Pick<ReminderCenterProps, "onOpen" | "onBreakeven" | "accountName">;

function ReminderRow({ item, onOpen, onBreakeven, accountName }: RowProps) {
  const info = reminderKind(item.kind);
  const account = item.accountId ? accountName?.(item.accountId) : undefined;
  const who = item.name || item.symbol || item.title;
  const text = item.body || (item.name || item.symbol ? item.title : "");
  const state = reminderState(item.state);
  const dest = DEST[item.target?.type ?? ""];
  const answers = (item.actions ?? []).filter((action): action is Extract<typeof action, { kind: "breakeven" }> => action.kind === "breakeven");
  return <li className="rc-item" data-unread={item.read ? undefined : ""} data-kind={item.kind}>
    <button type="button" className="rc-row" title={dest ? `去${dest}` : undefined} onClick={() => onOpen(item)}
      aria-label={`${info.label} ${who}${account ? ` ${account}` : ""}${text ? ` ${text}` : ""}${item.read ? "" : "，未读"}${dest ? `，点开去${dest}` : ""}`}>
      <time dateTime={item.createdAt}>{reminderClock(item.createdAt)}</time>
      <span className="rc-body">
        <span className="rc-line1">{item.read ? null : <i className="rc-dot" aria-hidden="true" />}<KindTag kind={item.kind} /><b>{who}</b>{account ? <span className="rc-account">{account}</span> : null}</span>
        {text ? <span className="rc-text">{text}</span> : null}
      </span>
      <ChevronRight className="rc-go" aria-hidden="true" />
    </button>
    {answers.length ? <div className="rc-extra">
      {answers.map(action => <button type="button" key={action.choice} className={`rc-button${action.choice === "move" ? "" : " ghost"}`}
        disabled={!onBreakeven} data-testid={`reminder-breakeven-${action.choice}`}
        onClick={() => onBreakeven?.(action.breakevenId, action.choice)}>{action.label}</button>)}
    </div> : state ? <div className="rc-extra"><span className="rc-state" data-tone={state.tone ?? undefined}>{state.text}</span></div> : null}
  </li>;
}

function Groups({ items, by, onOpen, onBreakeven, accountName, mobile }: { items: ReminderItem[]; by: "session" | "day"; mobile?: boolean } & Pick<ReminderCenterProps, "onOpen" | "onBreakeven" | "accountName">) {
  return <>{groupReminders(items, by).map(group => <div className="rc-group" key={group.key}>
    <div className="rc-group-label">{group.label}</div>
    <ul className={`rc-list${mobile ? " rc-card" : ""}`} aria-label={group.label}>
      {group.items.map(item => <ReminderRow key={item.id} item={item} onOpen={onOpen} onBreakeven={onBreakeven} accountName={accountName} />)}
    </ul>
  </div>)}</>;
}

/** 到价提醒 · 生效中: every active alert of the user's, 新建 for the shown contract, × deletes. */
export function AlertsBlock({ alerts, contract, onSwitchContract }: Pick<ReminderCenterProps, "contract" | "onSwitchContract"> & { alerts: PriceAlertsHandle }) {
  const [formOpen, setFormOpen] = useDisclosureSurface("reminder-alert-new", { available: alerts.available });
  const [seq, setSeq] = useState(0);
  return <section className="rc-alerts" aria-label="到价提醒 生效中">
    <div className="rc-block-head">
      <h3>到价提醒 · 生效中</h3><span className="rc-count">{alerts.active.length}</span>
      <HelpTip helpKey="reminder-alerts" label="到价提醒 · 生效中">{ALERT_HELP}</HelpTip>
      <span className="rc-spacer" />
      <button type="button" className="rc-button ghost" aria-expanded={formOpen} disabled={!contract?.symbol}
        onClick={() => { if (!formOpen) { alerts.refresh(); setSeq(value => value + 1); } setFormOpen(!formOpen); }}><Plus aria-hidden="true" />新建</button>
    </div>
    {formOpen && contract?.symbol ? <div className="rc-alert-form">
      <AlertForm key={`${contract.symbol}:${seq}`} alerts={alerts} symbol={contract.symbol} priceTick={contract.priceTick} lastPrice={contract.lastPrice} />
    </div> : null}
    {alerts.active.length ? <ul className="rc-alert-list">
      {alerts.active.map(alert => <li key={alert.id}>
        <button type="button" className="rc-alert-name" title={`切到 ${alert.symbol}`} aria-label={`${alert.name} ${alert.symbol}`}
          onClick={() => onSwitchContract?.(chartSymbolOf(alert.symbol))}>{alert.name}</button>
        <span className="rc-alert-line"><b data-direction={alert.direction}>{alert.direction === "above" ? "涨到" : "跌到"}</b> <span className="rc-mono">{alert.price}</span>
          {alert.note ? <i>{alert.note}</i> : null}</span>
        <button type="button" className="rc-alert-delete" aria-label={`删除提醒：${alertLine(alert)}`} title="删除" onClick={() => alerts.cancel(alert.id)}><X aria-hidden="true" /></button>
      </li>)}
    </ul> : <p className="rc-quiet">没有生效中的到价提醒</p>}
  </section>;
}

/** The filter, 全部已读, the alerts and the day's stream: the same body in the desktop popover and the phone page. */
export function ReminderCenterBody({ reminders, alerts, contract, filter, onFilter, onOpen, onBreakeven, onSwitchContract, accountName, mobile }: ReminderCenterProps) {
  const todayShown = filter === "unread" ? reminders.today.filter(item => !item.read) : reminders.today;
  const earlierShown = filter === "unread" ? reminders.earlier.filter(item => !item.read) : reminders.earlier;
  const empty = !todayShown.length && !earlierShown.length;
  // The list stands (read, or kept after a refresh that failed — Codex r4307948008): its ways on stay, with the error above.
  const listed = reminders.status === "ready" || (reminders.status === "error" && reminders.today.length > 0);
  const alertsBlock = alerts?.available ? <AlertsBlock alerts={alerts} contract={contract} onSwitchContract={onSwitchContract} /> : null;
  return <>
    {mobile ? <div className="rc-mobile-alerts rc-card">{alertsBlock}</div> : alertsBlock}
    <section className="rc-day" aria-label="当天提醒">
      <div className="rc-block-head rc-day-head"><h3>{tradingDayLabel(reminders.tradingDay)}</h3>
        <HelpTip helpKey="reminder-day" label="提醒记录的交易日">{DAY_HELP}</HelpTip></div>
      {reminders.status === "error" ? <p className="rc-quiet" role="status">{reminders.error}</p> : null}
      {reminders.status === "loading" && !reminders.today.length ? <p className="rc-quiet" role="status">正在读取提醒…</p>
        : empty && reminders.status !== "error" ? <div className="rc-empty"><CheckCheck aria-hidden="true" />
          <strong>{filter === "unread" ? "没有未读提醒" : "今天还没有提醒"}</strong>
          {filter === "unread" && reminders.today.length ? <button type="button" className="rc-button ghost" onClick={() => onFilter("all")}>看今天全部 {reminders.today.length} 条</button> : null}
        </div>
        : <Groups items={todayShown} by="session" onOpen={onOpen} onBreakeven={onBreakeven} accountName={accountName} mobile={mobile} />}
      {reminders.hasMoreToday && listed ? <>
        {reminders.moreError ? <p className="rc-quiet" role="status">{reminders.moreError}</p> : null}
        <button type="button" className="rc-button ghost rc-more" onClick={() => void reminders.loadMoreToday()}>继续加载当天提醒<ChevronDown aria-hidden="true" /></button>
      </> : null}
      {earlierShown.length ? <Groups items={earlierShown} by="day" onOpen={onOpen} onBreakeven={onBreakeven} accountName={accountName} mobile={mobile} /> : null}
      {filter === "all" && reminders.hasMoreEarlier && listed ? <>
        {reminders.earlierError ? <p className="rc-quiet" role="status">{reminders.earlierError}</p> : null}
        <button type="button" className="rc-button ghost rc-more" onClick={() => void reminders.loadEarlier()}>查看更早<ChevronDown aria-hidden="true" /></button>
      </> : null}
    </section>
  </>;
}

/** 未读 N / 全部 N and 全部已读 — the bar both forms put above the body. */
export function FilterBar({ reminders, filter, onFilter, mobile }: Pick<ReminderCenterProps, "reminders" | "filter" | "onFilter" | "mobile">) {
  const total = reminders.today.length + reminders.earlier.length;
  return <div className={mobile ? "rc-mobile-filters" : "rc-bar"}>
    <span className={mobile ? "rc-chips" : "rc-segmented"} role="radiogroup" aria-label="显示">
      <button type="button" role="radio" aria-checked={filter === "unread"} onClick={() => onFilter("unread")}>未读<span className="rc-mono">{reminders.unread}</span></button>
      <button type="button" role="radio" aria-checked={filter === "all"} onClick={() => onFilter("all")}>全部<span className="rc-mono">{total}</span></button>
    </span>
    <span className="rc-spacer" />
    <button type="button" className={mobile ? "rc-readall" : "rc-button ghost"} disabled={!reminders.unread} onClick={reminders.markAllRead}>
      {mobile ? null : <CheckCheck aria-hidden="true" />}全部已读</button>
  </div>;
}

/** The unread count over a bell or a tab icon (danger pair; hidden at zero). */
export function UnreadBadge({ count, className }: { count: number; className?: string }) {
  return count > 0 ? <span className={`rc-badge${className ? ` ${className}` : ""}`} aria-hidden="true">{count > 99 ? "99+" : count}</span> : null;
}

/**
 * 提醒记录 (电脑): the chart toolbar's bell (the badge counts unread). Since 2026-10-25 it opens the 提醒 panel on
 * 提醒记录 instead of a popover; `open` is that tab showing, so the `reminderCenter` command and a click agree.
 */
export function ReminderBell({ open, onOpenChange, unread }: { open: boolean; onOpenChange: (open: boolean) => void; unread: number }) {
  return <div className="rc-bell-wrap">
    <button type="button" className="rc-bell" aria-expanded={open}
      aria-label={`提醒记录（${unread} 条未读）`} title="提醒记录" onClick={() => onOpenChange(!open)}>
      <Bell aria-hidden="true" /><UnreadBadge count={unread} />
    </button>
  </div>;
}

/** 提醒 (手机): the full-screen page the AI tab's bell opens — the same three tabs as the computer's 提醒 panel. */
export function MobileReminders({ onClose, tabs, children }: { onClose: () => void; tabs: ReactNode; children: ReactNode }) {
  return <section className="fdm-overlay fdm-page rc-mobile reminders-panel" aria-label="提醒" data-testid="mobile-reminders" data-agent-target="mobile-reminders">
    <header className="fdm-head">
      <button type="button" className="fdm-back" aria-label="返回 AI 首页" onClick={onClose}><ChevronLeft /></button>
      <strong>提醒</strong>
    </header>
    {tabs}
    <main className="fdm-content rc-mobile-content">{children}</main>
  </section>;
}

/** AI 首页's 最新提醒: today's two newest, 全部 › opens the page. */
export function LatestReminders({ reminders, onOpen, onBreakeven, accountName, onAll }: Pick<ReminderCenterProps, "reminders" | "onOpen" | "onBreakeven" | "accountName"> & { onAll: () => void }): ReactNode {
  const latest = [...reminders.today].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt)).slice(0, 2);
  if (!latest.length) return null;
  return <section className="rc-latest rc-card" aria-label="最新提醒" data-agent-target="mobile-latest-reminders">
    <div className="rc-block-head"><h3>最新提醒</h3><span className="rc-spacer" />
      <button type="button" className="rc-button ghost" aria-label="全部提醒" onClick={onAll}>全部<ChevronRight aria-hidden="true" /></button></div>
    <ul className="rc-list" aria-label="最新提醒">
      {latest.map(item => <ReminderRow key={item.id} item={item} onOpen={onOpen} onBreakeven={onBreakeven} accountName={accountName} />)}
    </ul>
  </section>;
}
