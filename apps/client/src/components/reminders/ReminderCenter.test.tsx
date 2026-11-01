import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import type { PriceAlertsHandle } from "../../hooks/usePriceAlerts";
import type { ReminderFilter, ReminderItem, RemindersHandle } from "../../reminders";
import { resetUiSurfaces } from "../../uiSurfaces";
import { LatestReminders, MobileReminders, ReminderBell, type ReminderCenterProps } from "./ReminderCenter";
import { RemindersPanel, RemindersTabBody, RemindersTabs, type RemindersTab } from "./RemindersPanel";

const at = (clock: string, day = "2026-10-15") => new Date(`${day}T${clock}:00+08:00`).toISOString();
const item = (id: string, createdAt: string, over: Partial<ReminderItem> = {}): ReminderItem => ({
  id, kind: "market_close", tradingDay: "2026-10-15", createdAt, title: "收盘提醒", body: "", read: false, ...over });

const today: ReminderItem[] = [
  item("close", at("14:50"), { name: "螺纹2701 · 沪金2612", body: "收盘前 5 分钟，两笔持仓要过夜", target: { type: "chat", cardId: "c1" } }),
  item("be", at("14:31"), { kind: "breakeven", name: "螺纹2701", body: "涨到 3,080，已赚到 1 倍风险。把止损移到成本 3,060？",
    actions: [{ kind: "breakeven", label: "移到成本", breakevenId: "b1", choice: "move" }, { kind: "breakeven", label: "不用", breakevenId: "b1", choice: "decline" }] }),
  item("stop", at("14:05"), { kind: "stop_triggered", name: "铁矿2701", body: "跌破止损 772.0，平多 1 手", state: { text: "平仓单 已成交 771.5 ×1", tone: "ok" } }),
  item("price", at("13:42"), { kind: "price_alert", name: "沪金2612", body: "跌到 965.00 · 回踩前低", read: true }),
  item("brief", at("22:31", "2026-10-14"), { kind: "brief", name: "贵金属", body: "美国 9 月 CPI 高于预期", read: true, target: { type: "news" } }),
];

const handle = (over: Partial<RemindersHandle> = {}): RemindersHandle => ({
  available: true, status: "ready", error: "", tradingDay: "2026-10-15", today, hasMoreToday: false, moreError: "", earlierError: "", earlier: [], earlierShown: false, hasMoreEarlier: true,
  unread: 3, loadMoreToday: vi.fn(async () => ""), loadEarlier: vi.fn(async () => undefined), markRead: vi.fn(), markAllRead: vi.fn(), refresh: vi.fn(), ...over });

const alerts = (over: Partial<PriceAlertsHandle> = {}): PriceAlertsHandle => ({
  available: true, triggered: [], createError: "", notice: "", create: vi.fn(async () => true), cancel: vi.fn(), refresh: vi.fn(),
  active: [{ id: "a1", symbol: "SHFE.rb2701", name: "螺纹2701", direction: "above", price: "3100", note: "前高", status: "active", via: "ui",
    sessionId: null, createdAt: at("09:00"), triggeredAt: null, triggerPrice: null }], ...over });

/** The workspace's wiring: the bell opens the 提醒 panel on 提醒记录; the phone page has the same three tabs. */
function Center(props: Partial<ReminderCenterProps> & { start?: ReminderFilter; mobile?: boolean }) {
  const [filter, setFilter] = useState<ReminderFilter>(props.start ?? "all");
  const [tab, setTab] = useState<RemindersTab>("history");
  const base: ReminderCenterProps = { reminders: handle(), alerts: alerts(), contract: { symbol: "SHFE.rb2701", name: "螺纹2701", priceTick: 1, lastPrice: 3071 },
    filter, onFilter: setFilter, onOpen: vi.fn(), onSwitchContract: vi.fn(), ...props };
  const settings = <p>通用提醒的开关</p>;
  return props.mobile
    ? <MobileReminders onClose={vi.fn()} tabs={<RemindersTabs tab={tab} onTab={setTab} />}><RemindersTabBody tab={tab} center={base} settings={settings} mobile /></MobileReminders>
    : <>
      <ReminderBell open={tab === "history"} onOpenChange={open => setTab(open ? "history" : "mine")} unread={base.reminders.unread} />
      <RemindersPanel tab={tab} onTab={setTab} onClose={vi.fn()} center={base} settings={settings} />
    </>;
}
const history = () => screen.getByRole("tabpanel", { name: "提醒记录" });

afterEach(() => resetUiSurfaces());

it("the bell counts unread and opens 提醒记录, which lists the day by session with the kind tag, the state and the 保本 answers", () => {
  const onOpen = vi.fn(); const onBreakeven = vi.fn();
  render(<Center onOpen={onOpen} onBreakeven={onBreakeven} />);
  expect(screen.getByRole("button", { name: "提醒记录（3 条未读）" })).toHaveAttribute("aria-expanded", "true");
  const center = history();
  expect(within(center).getByRole("radio", { name: "未读 3" })).toHaveAttribute("aria-checked", "false");
  expect(within(center).getByRole("radio", { name: "全部 5" })).toHaveAttribute("aria-checked", "true");
  expect(within(center).getByText("10月15日 交易日")).toBeVisible();
  expect(within(center).getAllByRole("list").filter(list => list.classList.contains("rc-list")).map(list => list.getAttribute("aria-label")))
    .toEqual(["日盘 · 下午", "夜盘 · 10-14 晚"]);
  const close = within(center).getByRole("button", { name: /^收盘前 螺纹2701 · 沪金2612 收盘前 5 分钟，两笔持仓要过夜，未读，点开去对话$/ });
  fireEvent.click(close);
  expect(onOpen).toHaveBeenCalledWith(today[0]);
  expect(within(center).getByText("平仓单 已成交 771.5 ×1")).toHaveAttribute("data-tone", "ok");
  fireEvent.click(within(center).getByRole("button", { name: "移到成本" }));
  expect(onBreakeven).toHaveBeenCalledWith("b1", "move");
  expect(within(center).queryByRole("region", { name: "我的提醒" })).toBeNull();
  // 我的提醒: one list of what the user set, 新建提醒 opens 到某个价's form on the shown contract.
  fireEvent.click(screen.getByRole("tab", { name: "我的提醒" }));
  expect(screen.getByRole("button", { name: "提醒记录（3 条未读）" })).toHaveAttribute("aria-expanded", "false");
  const mine = screen.getByRole("tabpanel", { name: "我的提醒" });
  expect(within(mine).getByRole("region", { name: "我的提醒" })).toHaveTextContent("螺纹2701 · 价格到 3,100前高");
  fireEvent.click(within(mine).getByRole("button", { name: "新建提醒" }));
  expect(within(mine).getByRole("spinbutton", { name: "提醒价" })).toHaveValue(3071);
  // 通用提醒: the switches that were 设置 → 提醒.
  fireEvent.click(screen.getByRole("tab", { name: "通用提醒" }));
  expect(screen.getByRole("tabpanel", { name: "通用提醒" })).toHaveTextContent("通用提醒的开关");
});

it("未读 shows only the unread rows; with none left it says so and offers the day; 全部已读 and 查看更早 go to the Runtime", () => {
  const reminders = handle();
  const view = render(<Center reminders={reminders} start="unread" />);
  const center = history();
  expect(within(center).getAllByRole("listitem").filter(row => row.classList.contains("rc-item"))).toHaveLength(3);
  expect(within(center).queryByRole("button", { name: /查看更早/ })).toBeNull();
  fireEvent.click(within(center).getByRole("button", { name: "全部已读" }));
  expect(reminders.markAllRead).toHaveBeenCalled();
  view.unmount();
  const read = handle({ unread: 0, today: today.map(entry => ({ ...entry, read: true })) });
  render(<Center reminders={read} start="unread" />);
  const empty = history();
  expect(within(empty).getByText("没有未读提醒")).toBeVisible();
  expect(within(empty).getByRole("button", { name: "全部已读" })).toBeDisabled();
  fireEvent.click(within(empty).getByRole("button", { name: "看今天全部 5 条" }));
  fireEvent.click(within(empty).getByRole("button", { name: "查看更早" }));
  expect(read.loadEarlier).toHaveBeenCalled();
});

it("the phone page is the same list full screen; 最新提醒 shows today's two newest and 全部 opens the page", () => {
  render(<Center mobile />);
  const page = screen.getByRole("region", { name: "提醒" });
  expect(within(page).getByRole("button", { name: "返回 AI 首页" })).toBeVisible();
  expect(within(page).getByRole("tab", { name: "通用提醒" })).toBeVisible();
  expect(within(page).getByRole("radiogroup", { name: "显示" })).toBeVisible();
  const onAll = vi.fn();
  render(<LatestReminders reminders={handle()} onOpen={vi.fn()} onAll={onAll} />);
  const latest = screen.getByRole("region", { name: "最新提醒" });
  expect(within(latest).getAllByRole("listitem").map(row => row.getAttribute("data-kind"))).toEqual(["market_close", "breakeven"]);
  fireEvent.click(within(latest).getByRole("button", { name: "全部提醒" }));
  expect(onAll).toHaveBeenCalled();
  const { container } = render(<LatestReminders reminders={handle({ today: [] })} onOpen={vi.fn()} onAll={onAll} />);
  expect(container).toBeEmptyDOMElement();
});

it("a day read past its page limit offers 继续加载当天提醒 (#932 R5: never silently cut at 500)", () => {
  const more = handle({ hasMoreToday: true });
  render(<Center reminders={more} />);
  const center = history();
  fireEvent.click(within(center).getByRole("button", { name: "继续加载当天提醒" }));
  expect(more.loadMoreToday).toHaveBeenCalled();
});

it("a refresh that fails after the day was read keeps its rows, says so and keeps 继续加载当天提醒 (Codex r4307948008)", () => {
  render(<Center reminders={handle({ status: "error", error: "提醒读取失败，稍后会再试", hasMoreToday: true })} />);
  const center = history();
  expect(within(center).getByText("提醒读取失败，稍后会再试")).toBeVisible();
  expect(within(center).getByRole("button", { name: /收盘前 螺纹2701/ })).toBeVisible();
  expect(within(center).getByRole("button", { name: "继续加载当天提醒" })).toBeEnabled();
});

it("a failed 继续加载当天提醒 / 查看更早 says why beside the control, which stays to retry (Codex r4307841904)", () => {
  render(<Center reminders={handle({ hasMoreToday: true, moreError: "提醒读取失败，稍后会再试", earlierError: "服务端还没有提醒记录" })} />);
  const center = history();
  expect(within(center).getByText("提醒读取失败，稍后会再试")).toBeVisible();
  expect(within(center).getByText("服务端还没有提醒记录")).toBeVisible();
  expect(within(center).getByRole("button", { name: "继续加载当天提醒" })).toBeEnabled();
  expect(within(center).getByRole("button", { name: "查看更早" })).toBeEnabled();
});

it("#932 F7: with more than one account a 成交 row names its account on the first line, so two accounts' like orders differ", () => {
  const fills = [
    item("fill-a", at("10:01"), { kind: "fill", accountId: "tqkq-pool:a", name: "螺纹2701", body: "买开 1 手 3,071" }),
    item("fill-b", at("10:02"), { kind: "fill", accountId: "tqkq-pool:b", name: "螺纹2701", body: "买开 1 手 3,071" }),
  ];
  const names: Record<string, string> = { "tqkq-pool:a": "快期账户甲", "tqkq-pool:b": "快期账户乙" };
  const { unmount } = render(<Center reminders={handle({ today: fills })} accountName={id => names[id]} />);
  const center = history();
  expect(within(center).getByRole("button", { name: /^成交 螺纹2701 快期账户甲 买开/ })).toBeVisible();
  expect(within(center).getByRole("button", { name: /^成交 螺纹2701 快期账户乙 买开/ })).toBeVisible();
  unmount();
  // One account (no resolver given): nothing to tell apart, the row stays as it was.
  render(<Center reminders={handle({ today: fills })} />);
  expect(screen.queryByText("快期账户甲")).toBeNull();
});
