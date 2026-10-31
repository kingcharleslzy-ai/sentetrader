import { useEffect, useState } from "react";
import type { MarketWorkbenchSnapshot } from "../../api";
import { sameContract } from "../../api";
import type { OperationEvent } from "../../operationEvents";

export interface TradeFocus { openTime: number; closeTime: number; direction: string; symbol?: string }

/**
 * What the chart is focused on for review: selected trades, an operation event
 * located on the chart, the strategy filter and the explanation capability.
 * Workspace/runtime changes reset all focus. A symbol change preserves a selected
 * trade only on its own chart (including its mapped main contract); a period
 * change keeps a trade focus but drops an event focus.
 */
export function useChartFocus(workspaceScope: string | undefined, runtimeBaseUrl: string | undefined, selectedSymbol: string | undefined,
  duration: MarketWorkbenchSnapshot["durationSeconds"]) {
  const [tradeFocus, setTradeFocus] = useState<TradeFocus>();
  const [activeStrategy, setActiveStrategy] = useState("");
  const [selectedTrades, setSelectedTrades] = useState<string[]>();
  const [selectedEvent, setSelectedEvent] = useState<OperationEvent>();
  const [explanationSupported, setExplanationSupported] = useState(false);
  const [chartFocusMessage, setChartFocusMessage] = useState("");
  const clearChartFocus = () => {setSelectedTrades(undefined);setTradeFocus(undefined);setChartFocusMessage("");};
  useEffect(() => {setTradeFocus(undefined);setSelectedTrades(undefined);setSelectedEvent(undefined);setActiveStrategy("");setChartFocusMessage("");setExplanationSupported(false);}, [workspaceScope, runtimeBaseUrl]);
  useEffect(() => {
    setTradeFocus(value => value?.symbol && sameContract(value.symbol, selectedSymbol) ? value : undefined);
    setSelectedTrades(undefined);setSelectedEvent(undefined);setActiveStrategy("");setChartFocusMessage("");setExplanationSupported(false);
  }, [selectedSymbol]);
  // A period change drops an event or AI (`chartFocus` command, direction "agent") focus; a trade focus stays.
  useEffect(() => { setTradeFocus(value => value?.direction === "event" || value?.direction === "agent" ? undefined : value); setSelectedTrades(undefined); setChartFocusMessage(""); }, [duration]);
  return { tradeFocus, setTradeFocus, activeStrategy, setActiveStrategy, selectedTrades, setSelectedTrades, selectedEvent, setSelectedEvent,
    explanationSupported, setExplanationSupported, chartFocusMessage, setChartFocusMessage, clearChartFocus };
}

export type ChartFocus = ReturnType<typeof useChartFocus>;
