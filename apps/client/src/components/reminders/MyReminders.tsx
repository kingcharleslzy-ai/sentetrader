import { useCallback, useEffect, useState } from "react";
import { ChevronRight, Plus } from "lucide-react";
import { chartSymbolOf, requestJson } from "../../api";
import { loadStrategies, strategyError, validateAndStart, type SavedStrategy } from "../../strategyApi";
import { useDisclosureSurface, useUiSurface } from "../../uiSurfaces";
import { alertLine, type PriceAlertsHandle } from "../../hooks/usePriceAlerts";
import { AlertForm } from "../futures/PriceAlertsControl";
import { HelpTip } from "../Blocks";

const REFRESH_MS = 30_000;
const STATUS: Record<string, string> = { monitoring: "提醒中", paused: "已暂停" };
export const MY_REMINDERS_HELP = "你设的提醒都在这一张表里，满足时发一条提醒（对话、桌面通知、锁屏推送），不出卡、不下单。"
  + "到某个价：价格到了提醒一次，提醒过就从表里移走，记进提醒记录；点合约名切到那个合约的图。"
  + "碰到我画的线：在图上一条线的右键或样式条点“这条线被碰到时提醒我”，斜线按它此刻在图上的位置判断，挪线提醒跟着走，删线提醒也删。"
  + "按条件：在提醒编辑卡里选条件和提醒时段，判断、监控和推送和策略是同一套，同一个信号只提醒一次，也不出现在“我的策略”。";

type RunStatus = "monitoring" | "paused";
export type ReminderRow = { id: string; name: string; status: RunStatus | "idle"; symbol: string | null;
  runs: Array<{ runId: string; status: RunStatus }> };

/** A 提醒 (library strategy with `plan.reminderOnly`) and every live instance of it (one per contract it was started on):
 * 提醒中 while any instance monitors, so a row never reads 已暂停 while another contract is still watched (#932 T022). */
export function reminderRows(items: SavedStrategy[], executions: SavedStrategy[]): ReminderRow[] {
  return items.filter(item => item.definition.plan?.reminderOnly).map(item => {
    const live = executions.filter(entry => entry.definition.libraryStrategyId === item.strategyId && (entry.status === "monitoring" || entry.status === "paused"));
    const status: ReminderRow["status"] = live.some(run => run.status === "monitoring") ? "monitoring" : live.length ? "paused" : "idle";
    return { id: item.strategyId, name: item.name, status, symbol: live.length ? live.map(run => run.symbol).join("、") : null,
      runs: live.map(run => ({ runId: run.strategyId, status: run.status as RunStatus })) };
  });
}

/** "3,040" — the Runtime's tick-grid text with thousands grouped, decimals kept. */
const priceShown = (price: string) => Number.isFinite(Number(price)) ? Number(price).toLocaleString("zh-CN", { maximumFractionDigits: 6 }) : price;

export interface MyRemindersProps {
  baseUrl?: string;
  /** The chart's contract: a 条件提醒 starts on it; the price form defaults to it (`alertContract`). */
  contract?: string;
  chartTimeframe: number;
  /** Absent where the 策略编辑卡 cannot open (a visitor, a plan without strategies): 按条件 is then not offered and
   *  条件提醒 are listed and run only. */
  onNew?: () => void;
  onEdit?: (strategyId: string) => void;
}

/**
 * 我的提醒 (改版 2026-10-31, one list): every reminder the user set — 到某个价 and 碰到我画的线 (price alerts, /api/market/alerts)
 * and 按条件 (只提醒 strategies with `plan.reminderOnly`, run by the strategy routes) — one row each saying what it watches,
 * with its own actions. One 新建提醒 opens 到某个价's form (`disclosure: reminder-alert-new`) with 按条件… beside it (`onNew`,
 * the `strategyEditor {new, reminder}` command). Both backends stay as they were; this is how they read.
 */
export function MyReminders({ baseUrl, contract, chartTimeframe, onNew, onEdit, alerts, alertContract, onSwitchContract }: MyRemindersProps & {
  alerts: PriceAlertsHandle;
  alertContract?: { symbol?: string; priceTick?: number | null; lastPrice?: number | null };
  onSwitchContract?: (symbol: string) => void }) {
  const [rows, setRows] = useState<ReminderRow[]>();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [formOpen, setFormOpen] = useDisclosureSurface("reminder-alert-new", { available: alerts.available });
  const [seq, setSeq] = useState(0);
  const load = useCallback(async () => {
    if (!baseUrl) return;
    try {
      const result = await loadStrategies(baseUrl);
      setRows(reminderRows(result.items, result.executions ?? [])); setError("");
    } catch (cause) { setError(strategyError(cause, "提醒列表读取失败")); }
  }, [baseUrl]);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    window.addEventListener("ai-trader-strategies-changed", load);
    return () => { window.clearInterval(timer); window.removeEventListener("ai-trader-strategies-changed", load); };
  }, [load]);
  const act = async (row: ReminderRow, action: "start" | "pause" | "resume" | "delete") => {
    if (!baseUrl || busy) return;
    setBusy(row.id); setError("");
    try {
      if (action === "start") {
        if (!contract) throw new Error("先在图表上选一个合约");
        await validateAndStart(baseUrl, row.id, contract, chartTimeframe, undefined, { signalAction: "alert" });
      } else if (action === "resume") {
        for (const run of row.runs.filter(item => item.status === "paused")) {
          await requestJson(`${baseUrl}/api/strategies/${run.runId}/monitor/start`, { method: "POST", body: JSON.stringify({ signalAction: "alert" }) });
        }
      } else {
        // 暂停 / 删除 act on every instance; a 提醒 still running is paused first: the Runtime only deletes a strategy nothing monitors.
        for (const run of row.runs.filter(item => item.status === "monitoring")) {
          await requestJson(`${baseUrl}/api/strategies/${run.runId}/monitor/pause`, { method: "POST", body: "{}" });
        }
        if (action === "delete") await requestJson(`${baseUrl}/api/strategy-plans/${row.id}`, { method: "DELETE" });
      }
      window.dispatchEvent(new Event("ai-trader-strategies-changed"));
    } catch (cause) { setError(strategyError(cause, "没有做成，请稍后再试")); }
    finally { setBusy(null); }
  };
  useUiSurface("myReminders", { state: { status: rows ? "ready" : error ? "error" : "loading", items: rows ?? [], error: error || null } }, [rows, error]);
  // Without a Runtime there is nothing to read: the conditions part is simply empty, not loading.
  const conditions = baseUrl ? rows : [];
  const count = alerts.active.length + (conditions?.length ?? 0);
  return <section className="rc-alerts rc-my-reminders" aria-label="我的提醒" data-agent-target="reminders:mine">
    <div className="rc-block-head">
      <h3>我的提醒</h3><span className="rc-count">{count}</span>
      <HelpTip helpKey="reminder-mine" label="我的提醒">{MY_REMINDERS_HELP}</HelpTip>
      <span className="rc-spacer" />
      <button type="button" className="rc-button" aria-expanded={formOpen}
        onClick={() => { if (!formOpen) { alerts.refresh(); setSeq(value => value + 1); } setFormOpen(!formOpen); }}><Plus aria-hidden="true" />新建提醒</button>
    </div>
    {formOpen ? <div className="rc-alert-form rc-new">
      <div className="rc-new-head"><strong>到某个价</strong><span className="rc-spacer" />
        {onNew ? <button type="button" className="rc-button ghost" onClick={() => { setFormOpen(false); onNew(); }}>按条件…<ChevronRight aria-hidden="true" /></button> : null}</div>
      {alertContract?.symbol
        ? <AlertForm key={`${alertContract.symbol}:${seq}`} alerts={alerts} symbol={alertContract.symbol} priceTick={alertContract.priceTick} lastPrice={alertContract.lastPrice} />
        : <p className="rc-quiet">先在图表上选一个合约</p>}
    </div> : null}
    {error ? <p className="rc-quiet" role="alert">{error}</p> : null}
    {count ? <ul className="rc-alert-list">
      {alerts.active.map(alert => <li key={alert.id} data-kind={alert.drawingId ? "line" : "price"}>
        <span className="rc-alert-line">
          <button type="button" className="rc-alert-name" title={`切到 ${alert.symbol}`} aria-label={`${alert.name} ${alert.symbol}`}
            onClick={() => onSwitchContract?.(chartSymbolOf(alert.symbol))}>{alert.name}</button>
          {" · "}{alert.drawingId ? "碰到我画的线" : <>价格到 <span className="rc-mono">{priceShown(alert.price)}</span></>}
          {alert.note ? <i>{alert.note}</i> : null}
        </span>
        <span className="rc-reminder-actions"><b className="rc-row-status" data-status="monitoring">提醒中</b>
          <button type="button" className="rc-button ghost danger" aria-label={`删除提醒：${alertLine(alert)}`} onClick={() => alerts.cancel(alert.id)}>删除</button>
        </span>
      </li>)}
      {(conditions ?? []).map(row => <li key={row.id} data-kind="condition">
        <span className="rc-alert-line">{row.symbol ? <><span className="rc-mono">{row.symbol}</span>{" · "}</> : null}{row.name}</span>
        <span className="rc-reminder-actions">
          <b className="rc-row-status" data-status={row.status}>{STATUS[row.status] ?? "未开始"}</b>
          {row.status === "monitoring" ? <button type="button" className="rc-button ghost" disabled={busy !== null} onClick={() => void act(row, "pause")}>暂停</button>
            : row.status === "paused" ? <button type="button" className="rc-button ghost" disabled={busy !== null} onClick={() => void act(row, "resume")}>继续</button>
            : <button type="button" className="rc-button ghost" disabled={busy !== null || !contract} title={contract ? `在 ${contract} 上开始提醒` : "先在图表上选一个合约"}
              onClick={() => void act(row, "start")}>开始</button>}
          {onEdit ? <button type="button" className="rc-button ghost" disabled={busy !== null} onClick={() => onEdit(row.id)}>编辑</button> : null}
          <button type="button" className="rc-button ghost danger" disabled={busy !== null} aria-label={`删除提醒：${row.name}`} onClick={() => void act(row, "delete")}>删除</button>
        </span>
      </li>)}
    </ul>
      : !conditions ? (error ? null : <p className="rc-quiet" role="status">正在读取…</p>)
      : <p className="rc-quiet">还没有提醒：点“新建提醒”，价格到了或条件满足时提醒你。</p>}
  </section>;
}
