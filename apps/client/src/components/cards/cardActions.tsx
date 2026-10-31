import type { CardAction } from "@ai-trader/ui-events";

/** What a structured card's buttons can do in this window: send a prompt as the user's own message,
 *  or apply one existing UI command. Missing (tests, guests) renders the buttons disabled. */
export interface CardActionHandlers {
  askAi?: (text: string) => void;
  applyUi?: (changes: Record<string, unknown>) => void;
  /** 保本提醒 (2026-10-06): 移到成本 / 不用 through the question's own business route — never an order. */
  breakeven?: (breakevenId: string, choice: "move" | "decline") => void;
}

/** The only UI command keys a card may apply: open a panel, the research desk or a tab, switch the
 *  contract or the view, open 复盘 on a trading day (今日复盘卡's 看复盘, 2026-10-06). Anything else a
 *  card carries is dropped here, so a card can never reach trading, settings or preferences. */
const CARD_UI_KEYS = ["panel", "news", "positionsTab", "ticketMode", "strategyTab", "symbol", "view", "reviewDay"] as const;

export function pickCardUiChanges(changes: Record<string, unknown> | null | undefined): Record<string, unknown> {
  const picked: Record<string, unknown> = {};
  for (const key of CARD_UI_KEYS) if (changes && key in changes) picked[key] = changes[key];
  return picked;
}

/** "问 AI…" names every ask button (the coverage audit and the observation snapshot match on it). */
export const askLabel = (label: string) => label.startsWith("问 AI") ? label : `问 AI · ${label}`;

/** A change in percent for a card: two decimals, "+" on a rise, no "-0.00" (the Runtime writes answer / data
 *  card figures with the same rule, answer_cards.signed). Non-finite input shows "—". */
export function signedPercent(value: number | null | undefined, digits = 2): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const rounded = Number(value.toFixed(digits));
  if (rounded === 0) return `${(0).toFixed(digits)}%`;
  return `${rounded > 0 ? "+" : ""}${rounded.toFixed(digits)}%`;
}

/** HH:mm in the viewer's time zone; "—" when the time is missing or unreadable. */
export function cardClock(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
}

/** Full local date and time for a card's 详情. */
export function cardDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

/** A card's own buttons (never an order): ask = send its prompt, ui = apply its whitelisted command. */
export function CardActionButtons({ actions, handlers }: { actions?: CardAction[]; handlers: CardActionHandlers }) {
  if (!actions?.length) return null;
  return <>{actions.map((action, index) => {
    if (action.kind === "breakeven") {
      return <button type="button" className={`${action.choice === "move" ? "primary-button accent" : "ghost-button"} sc-action`}
        key={`${action.label}-${index}`} data-testid={`breakeven-${action.choice}`}
        disabled={!handlers.breakeven} onClick={() => handlers.breakeven?.(action.breakevenId, action.choice)}>{action.label}</button>;
    }
    if (action.kind === "ask") {
      return <button type="button" className="ghost-button sc-action" key={`${action.label}-${index}`}
        aria-label={askLabel(action.label)} title={`发送：${action.prompt}`}
        disabled={!handlers.askAi} onClick={() => handlers.askAi?.(action.prompt)}>{action.label}</button>;
    }
    const changes = pickCardUiChanges(action.changes);
    const usable = Boolean(handlers.applyUi) && Object.keys(changes).length > 0;
    return <button type="button" className="ghost-button sc-action" key={`${action.label}-${index}`}
      title={`界面命令：${JSON.stringify(changes)}`}
      disabled={!usable} onClick={() => handlers.applyUi?.(changes)}>{action.label}</button>;
  })}</>;
}
