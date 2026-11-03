/**
 * 今日作战表 put together on this device (owner 2026-11-03, 按人的拼装在本地): the Runtime pushes each 自选品种's
 * shared row (`sheet-row?product=`, the row GET /api/research/sheet ranks); which products, their order and which of
 * the user's 打法 show up near the levels are the user's own and are done here, exactly as battle_sheet.py `sheet()`
 * and `playbooks_for()` do for that read (kept for guests, fallback and agents). Change one, change the other.
 */
import type { SheetReport, SheetRow } from "./components/BattleSheet";
import { isForeignProductKey } from "./components/futures/support";

export const SHEET_LIMIT = 12;
const CLOSE_PERCENT = 1.0;
const SCENARIO_WORDS: Record<"prev" | "opening" | "vwap" | "news", readonly string[]> = {
  prev: ["昨高", "昨低", "前高", "前低", "昨日", "昨天"],
  opening: ["开盘区间", "开盘", "区间突破", "ORB", "orb"],
  vwap: ["均价", "VWAP", "vwap", "分时均"],
  news: ["消息", "新闻", "数据", "事件", "公告"],
};
const ANY_LEVEL_WORDS = ["突破", "回踩", "假突破", "反抽", "支撑", "压力"];
const LEVEL_SCENARIO: Record<string, "prev" | "opening" | "vwap"> = {
  prev_high: "prev", prev_low: "prev", opening_high: "opening", opening_low: "opening", vwap: "vwap" };

/** A pushed row: everything the sheet shows but the user's 打法. */
export type SharedSheetRow = Omit<SheetRow, "playbooks">;
type PlaybookText = { id: string; name: string; entryRules?: string | null };

/** Python's str() of a float (the Runtime's wording): 2.0, not 2. */
const pyNumber = (value: number) => Number.isInteger(value) ? value.toFixed(1) : String(value);

/** playbooks_for: the 打法 whose name or entry rules mention a scenario in play, at most three. */
export function playbooksFor(playbooks: readonly PlaybookText[], levels: SheetRow["levels"], strength: string): SheetRow["playbooks"] {
  const close = levels.filter(level => typeof level.distancePercent === "number" && level.distancePercent <= CLOSE_PERCENT)
    .sort((a, b) => a.distancePercent! - b.distancePercent!);
  const has = (text: string, words: readonly string[]) => words.some(word => text.includes(word));
  const matched: SheetRow["playbooks"] = [];
  for (const playbook of playbooks) {
    const text = `${playbook.name || ""} ${playbook.entryRules || ""}`;
    const level = close.find(item => has(text, SCENARIO_WORDS[LEVEL_SCENARIO[item.key]]));
    let why = level ? `离${level.label} ${pyNumber(level.distancePercent!)}%` : null;
    // A generic 突破 / 回踩 fits any close level, unless the setup names its own level (昨低反抽 waits for 昨低).
    const named = (["prev", "opening", "vwap"] as const).some(scenario => has(text, SCENARIO_WORDS[scenario]));
    if (why === null && close.length && !named && has(text, ANY_LEVEL_WORDS)) why = `离${close[0].label} ${pyNumber(close[0].distancePercent!)}%`;
    if (why === null && strength !== "无" && has(text, SCENARIO_WORDS.news)) why = `消息${strength}`;
    if (why !== null) matched.push({ id: playbook.id, name: playbook.name, why });
  }
  return matched.slice(0, 3);
}

/** sheet(): the domestic 自选 (first SHEET_LIMIT) ranked by score, then the nearest level, then 自选 order, each with
 *  its 打法; undefined until every shown product's row has arrived. */
export function assembleSheet(products: readonly string[], rows: ReadonlyMap<string, SharedSheetRow>, playbooks: readonly PlaybookText[],
  now = new Date()): SheetReport | undefined {
  const domestic = products.filter(key => !isForeignProductKey(key));
  const shown = domestic.slice(0, SHEET_LIMIT);
  if (shown.some(key => !rows.has(key))) return undefined;
  const nearest = (row: SharedSheetRow) => row.nearest ? row.nearest.distancePercent : 999;
  const ranked = shown.map(key => rows.get(key)!)
    .sort((a, b) => b.score - a.score || nearest(a) - nearest(b) || shown.indexOf(a.product) - shown.indexOf(b.product));
  return { generatedAt: now.toISOString().replace(/\.\d{3}Z$/, "+00:00"),
    rows: ranked.map(row => ({ ...row, playbooks: playbooksFor(playbooks, row.levels, row.news.strength) })),
    foreign: products.filter(isForeignProductKey), over: Math.max(0, domestic.length - SHEET_LIMIT), playbookCount: playbooks.length };
}
