import { useCallback, useEffect, useState } from "react";
import { Plus } from "lucide-react";
import { requestJson } from "../../api";
import { loadStrategies, strategyError, validateAndStart, type SavedStrategy } from "../../strategyApi";
import { useUiSurface } from "../../uiSurfaces";
import { HelpTip } from "../Blocks";

const REFRESH_MS = 30_000;
const STATUS: Record<string, string> = { monitoring: "提醒中", paused: "已暂停" };
export const MY_REMINDERS_HELP = "这里的提醒是“只提醒”的策略：在策略编辑卡的提醒模式里选条件和提醒时段，条件满足时发一条提醒（对话、桌面通知、锁屏推送，"
  + "跟通用提醒里“策略出卡与自动下单”的开关走），不出卡、不下单，也不出现在“我的策略”。判断、监控和推送和策略是同一套；同一个信号只提醒一次。";

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

/**
 * 我的提醒 · 条件提醒 (侧栏重排第 6 批, owner 2026-10-25): the user's 提醒 — strategies saved in the editor card's 提醒模式 — with
 * 开始 / 暂停 / 继续 / 编辑 / 删除. 新建提醒 opens the card in 提醒模式 (`onNew`), 编辑 opens it on that 提醒 (`onEdit`).
 * A 提醒 starts on `contract` (the chart's) as 只提醒; the monitor, rules and push are the strategy's own.
 */
export function MyReminders({ baseUrl, contract, chartTimeframe, onNew, onEdit }: { baseUrl?: string; contract?: string; chartTimeframe: number;
  /** Absent where the 策略编辑卡 cannot open (a visitor, a plan without strategies): 条件提醒 are then listed and run only. */
  onNew?: () => void; onEdit?: (strategyId: string) => void }) {
  const [rows, setRows] = useState<ReminderRow[]>();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
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
  return <section className="rc-alerts rc-my-reminders" aria-label="条件提醒" data-agent-target="reminders:conditions">
    <div className="rc-block-head">
      <h3>条件提醒</h3><span className="rc-count">{rows?.length ?? 0}</span>
      <HelpTip helpKey="reminder-conditions" label="条件提醒">{MY_REMINDERS_HELP}</HelpTip>
      <span className="rc-spacer" />
      {onNew ? <button type="button" className="rc-button" disabled={!baseUrl} onClick={onNew}><Plus aria-hidden="true" />新建提醒</button> : null}
    </div>
    {error ? <p className="rc-quiet" role="alert">{error}</p> : null}
    {!rows ? (error ? null : <p className="rc-quiet" role="status">正在读取…</p>)
      : rows.length ? <ul className="rc-alert-list">{rows.map(row => <li key={row.id}>
        <span className="rc-alert-name">{row.name}</span>
        <span className="rc-alert-line"><b data-status={row.status}>{STATUS[row.status] ?? "未开始"}</b>{row.symbol ? <span className="rc-mono"> {row.symbol}</span> : null}</span>
        <span className="rc-reminder-actions">
          {row.status === "monitoring" ? <button type="button" className="rc-button ghost" disabled={busy !== null} onClick={() => void act(row, "pause")}>暂停</button>
            : row.status === "paused" ? <button type="button" className="rc-button ghost" disabled={busy !== null} onClick={() => void act(row, "resume")}>继续</button>
            : <button type="button" className="rc-button ghost" disabled={busy !== null || !contract} title={contract ? `在 ${contract} 上开始提醒` : "先在图表上选一个合约"}
              onClick={() => void act(row, "start")}>开始</button>}
          {onEdit ? <button type="button" className="rc-button ghost" disabled={busy !== null} onClick={() => onEdit(row.id)}>编辑</button> : null}
          <button type="button" className="rc-button ghost danger" disabled={busy !== null} aria-label={`删除提醒：${row.name}`} onClick={() => void act(row, "delete")}>删除</button>
        </span>
      </li>)}</ul>
      : <p className="rc-quiet">{onNew ? "还没有条件提醒：点“新建提醒”，选好条件和时段，满足时提醒你。" : "还没有条件提醒。"}</p>}
  </section>;
}
