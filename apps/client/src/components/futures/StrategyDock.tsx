import { Bell, ListTree, PanelRightClose } from "lucide-react";
import { cloneElement, isValidElement } from "react";
import type { ReactElement } from "react";
import type { MarketQuoteSnapshot } from "../../api";
import { useStrategyEditor } from "../../strategyEditorStore";
import type { AskAi } from "../AiMenu";
import type { FuturesDecisionWorkspaceProps } from "./props";
import type { StrategyTab } from "./support";

interface Props {
  workspace: FuturesDecisionWorkspaceProps;
  quote: MarketQuoteSnapshot | undefined;
  onClose: () => void;
  /** 我的策略 / 正在运行 and the 结果页 (`strategyTab` command), owned by the workspace. */
  tab: StrategyTab;
  onTabChange: (tab: StrategyTab) => void;
  onStrategySelected: (strategyId: string) => void;
  /** 设置 → 交易 → AI 自主执行: 自动下单 is only offered while it is on. */
  autonomy: boolean | undefined;
  /** 对象级 AI: an "AI ▾" item's question with its focus, and the reason lines greying the
   *  signal items (strategy-signals) and 回测与体检 (strategy-backtests); null = usable. */
  onAskAi: AskAi;
  explainBlocked: string | null;
  backtestBlocked: string | null;
  /** 规则回测 · 在图上标出买卖点: switch the chart to the backtest's contract and period. */
  onShowChart?: (symbol: string, durationSeconds: number) => void;
  /** 提醒模式 (第 6 批): back, 放弃 or 开始提醒 on a 提醒 returns to the 提醒 panel's 我的提醒. */
  onShowReminders?: () => void;
}

/**
 * 策略面板 (策略页改版第八批, 2026-10-08): the header names the chart's contract; the workbench below
 * holds 我的策略 (the ask box that replaced ＋ 新建策略, `disclosure: strategy-new`) and 正在运行.
 */
export function StrategyDock({ workspace, quote, onClose, tab, onTabChange, onStrategySelected, autonomy, onAskAi, explainBlocked, backtestBlocked, onShowChart, onShowReminders }: Props) {
  const panel = workspace.strategyPanel && isValidElement(workspace.strategyPanel)
    ? cloneElement(workspace.strategyPanel as ReactElement<Record<string, unknown>>, {
      tab, onTabChange, onSelectionChange: onStrategySelected,
      instruments: workspace.instruments, autonomy, onAskAi, explainBlocked, backtestBlocked, onShowChart, onShowReminders,
      // 上线运行: reminders and strategy cards go to the conversation the instance was started from.
      sessionId: workspace.chatSessionId })
    : workspace.strategyPanel;
  // 新建 / 编辑提醒 (改版第一步 2026-10-31) borrow this panel's editor card; the panel then reads as 提醒.
  const editor = useStrategyEditor();
  const reminder = editor.open && editor.reminder;
  return (
    <section className="fdw-ai open fdw-strategy-dock" aria-label={reminder ? "提醒面板" : "策略面板"} data-agent-target="panel:strategy">
      <header>
        <div>{reminder ? <Bell /> : <ListTree />}<span><strong>{reminder ? "提醒" : "策略"}</strong><small>{quote?.symbol ?? "未选择合约"}</small></span></div>
        <div className="fdw-ai-head-actions">
          <button type="button" aria-label={reminder ? "收起提醒" : "收起策略"} onClick={() => onClose()}><PanelRightClose /></button>
        </div>
      </header>
      <div className="fdw-ai-scroll">
        {panel}
      </div>
    </section>
  );
}
