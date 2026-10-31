import { useSyncExternalStore } from "react";
import { requestJson } from "./api";
import type { InstrumentScope, SavedStrategy } from "./strategyApi";

/**
 * 策略模板库 (策略积木·第二期 A, 2026-09-25): the Runtime's template library (`GET /api/strategy-templates`,
 * `GET …/{id}` with the template's default plan, `POST …/{id}/instantiate`). Since 2026-10-08 a template
 * is the 策略编辑卡's 起点 (从模板 ▾); the gallery and the parameter page are gone. The catalog is kept
 * once per Runtime for the card's dropdown and the 结果页's 来自模板 / 恢复模板默认.
 */
/** 说人话 (2026-09-25): the “?” beside a parameter — what it is, what raising / lowering it does. */
export interface ParamHelp { what: string; up: string; down: string }
/** 价差模板的合约腿 (2026-09-29): `type: "contract"` 是一个具体合约代码（min / max / step 为 null），其余是数字参数。 */
/** `optional` (10-08 当日亏损上限): a number the user may leave empty — default null, empty = 不设. */
export interface TemplateParam { key: string; label: string; type?: "number" | "contract"; default: number | string | null; optional?: boolean;
  min?: number | null; max?: number | null; step?: number | null; unit: string; integer: boolean; targets: string[];
  /** Absent on an older Runtime: the “?” is then not shown. */
  help?: ParamHelp }
export interface StrategyTemplate {
  id: string; name: string; category: string; categoryLabel: string; scenario: string;
  /** 说人话 (2026-09-25): one jargon-free sentence, 新手 / 进阶 and 日内 / 会隔夜; absent on an older Runtime. */
  plainSummary?: string; level?: "新手" | "进阶"; holding?: "日内" | "会隔夜";
  source: { label: string; url: string }; recommendedTimeframe: number; approximations: string[];
  params: TemplateParam[]; constraints: { less: [string, string]; message: string }[];
}
export interface TemplateCatalog { categories: { id: string; label: string }[]; items: StrategyTemplate[] }

let catalog: { url: string; value?: TemplateCatalog; error?: string; loading?: Promise<TemplateCatalog> } | null = null;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach(listener => listener());
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
const catalogSnapshot = () => catalog;
export function useTemplateCatalog(): TemplateCatalog | undefined { return useSyncExternalStore(subscribe, catalogSnapshot, catalogSnapshot)?.value; }

/** The library once per Runtime URL (templates ship with the Runtime and do not change while it runs). */
export async function loadTemplates(rootUrl: string): Promise<TemplateCatalog> {
  if (catalog?.url === rootUrl && catalog.value) return catalog.value;
  if (catalog?.url === rootUrl && catalog.loading) return catalog.loading;
  const loading = requestJson<TemplateCatalog>(`${rootUrl}/api/strategy-templates`);
  catalog = { url: rootUrl, loading };
  notify();
  try {
    const value = await loading;
    if (catalog?.url === rootUrl) { catalog = { url: rootUrl, value }; notify(); }
    return value;
  } catch (error) {
    if (catalog?.url === rootUrl) { catalog = { url: rootUrl, error: "模板库读取失败，请检查连接后重试" }; notify(); }
    throw error;
  }
}
export function templateById(id: string | null | undefined): StrategyTemplate | undefined {
  return id ? catalog?.value?.items?.find(item => item.id === id) : undefined;
}

const text = (value: number | string | null) => value === null ? "" : typeof value === "string" ? value : String(Number(value.toFixed(6)));
/** A parameter's value as shown read-only: null (an optional parameter left empty) is 不设. */
export const templateValueText = (value: number | string | null | undefined, unit: string) =>
  value == null ? "不设" : `${typeof value === "number" ? text(value) : value}${unit ? ` ${unit}` : ""}`;

/** 恢复模板默认: the template's defaults saved as a strategy (a new version of the same name with `baseStrategyId`). */
export function instantiateTemplate(rootUrl: string, templateId: string, body: { params?: Record<string, number | string | null>; name?: string; chartSymbol?: string; timeframe?: number; instrumentScope?: InstrumentScope; baseStrategyId?: string }) {
  return requestJson<SavedStrategy>(`${rootUrl}/api/strategy-templates/${encodeURIComponent(templateId)}/instantiate`, { method: "POST", body: JSON.stringify(body) });
}

/** 空状态的常用模板 (2026-10-08): 笔数 · 胜率 of a template's default parameters on one contract. */
export interface TemplateFigures { trades: number; winRate: number | null }
/** 我的策略's empty state shows these first (the classic trend, breakout, reversal and day-range ones). */
export const COMMON_TEMPLATE_IDS = ["double_ma", "donchian_turtle", "rsi_reversal", "dual_thrust"] as const;
export const COMMON_PREVIEW_DAYS = 60;
const previews = new Map<string, Promise<TemplateFigures | null>>();
/**
 * The template's preview backtest (`POST /api/strategy-templates/{id}/preview`, nothing saved) over the last
 * 60 trading days, once per Runtime, contract and template in this window; a failure is null (and kept: the
 * row then shows only its sentence).
 */
export function templateFigures(rootUrl: string, templateId: string, symbol: string): Promise<TemplateFigures | null> {
  const key = `${rootUrl}|${symbol}|${templateId}`;
  let pending = previews.get(key);
  if (!pending) {
    pending = requestJson<{ backtest?: { summary?: { trades?: number; winRate?: number | null } } }>(
      `${rootUrl}/api/strategy-templates/${encodeURIComponent(templateId)}/preview`, { method: "POST", body: JSON.stringify({ symbol, tradingDays: COMMON_PREVIEW_DAYS }) })
      .then(reply => reply.backtest?.summary?.trades == null ? null : { trades: reply.backtest.summary.trades, winRate: reply.backtest.summary.winRate ?? null })
      .catch(() => null);
    previews.set(key, pending);
  }
  return pending;
}

/** Test helper. */
export function resetStrategyTemplates() { catalog = null; previews.clear(); notify(); }
