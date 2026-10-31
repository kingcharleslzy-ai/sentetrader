import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { vi } from "vitest";
import { closeCheckTime } from "../../marketSession";
import { buildFlowItems } from "../futures/aiFlow";
import { buildMobileFeed } from "./feedItems";
import { MobileHome, type WatchInfo } from "./MobileHome";
import { notifyProducts, notifyRow, type NotifyFilter } from "./notifyItems";

const brief = (id: string, product: string, productName: string, text: string, minutes: number) => ({
  id, kind: "news" as const, text, source: "fixture", product, productName, symbol: `${product}2601`,
  publishedAt: new Date(Date.UTC(2026, 9, 5, 1, minutes)).toISOString(), expiresAt: "2026-10-06T00:00:00Z" });

const rows = buildMobileFeed({ turns: [], briefs: buildFlowItems({ briefs: [
  brief("b1", "SHFE.rb", "螺纹钢", "螺纹库存回落", 1), brief("b2", "DCE.i", "铁矿石", "港口到货增加", 2), brief("b3", "SHFE.rb", "螺纹钢", "钢厂限产", 3),
] }) }).map(notifyRow);

const watch: WatchInfo = { positions: ["甲醇空单 6 手"], alerts: ["螺纹 3188"], strategies: ["均线金叉 · 出卡确认"], movement: true,
  next: ["14:55 收盘前持仓检查", "15:15 今日复盘"], closed: false };
const now = Date.UTC(2026, 9, 5, 2, 0);

function Home({ filterStart = "all", ...rest }: Partial<Parameters<typeof MobileHome>[0]> & { filterStart?: NotifyFilter }) {
  const [filter, setFilter] = useState<NotifyFilter>(filterStart);
  const [product, setProduct] = useState<string | null>(null);
  return <div className="fdm-content"><MobileHome rows={rows} pending={[]} renderCard={() => null} quickPending={false} now={now}
    today={{ positions: 1, floating: 860, reminders: 0, unreadReminders: 0 }} onPositions={vi.fn()} watch={watch} judgments={[]} onJudgment={vi.fn()}
    filter={filter} onFilter={setFilter} products={notifyProducts(rows)} product={product} onProduct={setProduct}
    onOpen={vi.fn()} onReminders={vi.fn()} focus={0} {...rest} /></div>;
}

const titles = () => within(screen.getByRole("list", { name: "时间线" })).getAllByRole("button").map(button => button.textContent);

it("时间线（原通知）：每条简报带品种，AI 简报下按出现的品种筛", () => {
  render(<Home />);
  expect(titles()).toEqual([expect.stringContaining("螺纹钢"), expect.stringContaining("铁矿石"), expect.stringContaining("螺纹钢")]);
  expect(screen.queryByRole("group", { name: "简报品种" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "AI 简报" }));
  const chips = screen.getByRole("group", { name: "简报品种" });
  expect(within(chips).getAllByRole("button").map(button => button.textContent)).toEqual(["全部品种", "螺纹钢", "铁矿石"]);
  fireEvent.click(within(chips).getByRole("button", { name: "铁矿石" }));
  expect(titles()).toHaveLength(1);
  expect(titles()[0]).toContain("港口到货增加");
  fireEvent.click(within(chips).getByRole("button", { name: "全部品种" }));
  expect(titles()).toHaveLength(3);
});

it("订阅品种在前；订阅了还没有内容的品种说明还没有简报", () => {
  const products = notifyProducts(rows, [{ product: "SHFE.ru", name: "橡胶" }, { product: "SHFE.rb", name: "螺纹钢" }]);
  expect(products.map(item => item.name)).toEqual(["橡胶", "螺纹钢", "铁矿石"]);
  render(<MobileHome rows={rows} pending={[]} renderCard={() => null} quickPending={false} now={now}
    today={{ positions: 0, floating: null, reminders: 0, unreadReminders: 0 }} onPositions={vi.fn()} watch={watch} judgments={[]} onJudgment={vi.fn()}
    filter="brief" onFilter={vi.fn()} products={products} product="SHFE.ru" onProduct={vi.fn()} onOpen={vi.fn()} onReminders={vi.fn()} focus={0} />);
  expect(screen.queryByRole("list", { name: "时间线" })).toBeNull();
  expect(screen.getByText("橡胶还没有简报")).toBeVisible();
});

it("它正在盯着：没有待确认、今天也没有提醒时显示；有待确认或今天有提醒时收起；休市与什么都没有各有一句", () => {
  const view = render(<Home />);
  const block = screen.getByTestId("mobile-watch");
  expect(block).toHaveTextContent("1 笔持仓甲醇空单 6 手");
  expect(block).toHaveTextContent("1 个到价提醒螺纹 3188");
  expect(block).toHaveTextContent("1 个策略运行中均线金叉 · 出卡确认");
  expect(block).toHaveTextContent("盘中异动持仓合约，每分钟一次");
  expect(block).toHaveTextContent("下一次：14:55 收盘前持仓检查15:15 今日复盘");
  view.rerender(<Home today={{ positions: 1, floating: 860, reminders: 2, unreadReminders: 1 }} />);
  expect(screen.queryByTestId("mobile-watch")).toBeNull();
  expect(within(screen.getByRole("group", { name: "今天概况" })).getByRole("button", { name: /^今天提醒/ })).toHaveTextContent("2 条1 条未读");
  // The three tiles are named for screen readers and the observation snapshot (10-06 实测：名字为空).
  const tiles = within(screen.getByRole("group", { name: "今天概况" })).getAllByRole("button").map(button => button.getAttribute("aria-label"));
  expect(tiles).toEqual(["持仓 1 笔，浮盈 +860", "待确认 0 张", "今天提醒 2 条，1 条未读"]);
  view.rerender(<Home quickPending />);
  expect(screen.queryByTestId("mobile-watch")).toBeNull();
  view.rerender(<Home watch={{ ...watch, closed: true }} />);
  expect(screen.getByTestId("mobile-watch")).toHaveTextContent("休市中，开盘后继续盯");
  expect(screen.getByTestId("mobile-watch")).not.toHaveTextContent("收盘前持仓检查");
  view.rerender(<Home watch={{ positions: [], alerts: [], strategies: [], movement: true, next: ["15:15 今日复盘"], closed: false }} />);
  expect(screen.getByTestId("mobile-watch")).toHaveTextContent("还没有持仓、提醒或策略");
  expect(screen.getByTestId("mobile-watch")).not.toHaveTextContent("盘中异动");
});

it("今日判断只是一行品种胶囊：点一个交给调研；今天提醒打开提醒页，最新提醒在时间线上面", () => {
  const onJudgment = vi.fn();
  const onReminders = vi.fn();
  render(<Home judgments={[{ product: "SHFE.rb", name: "螺纹钢", stance: "震荡", symbol: "SHFE.rb2601" }, { product: "DCE.i", name: "铁矿石", stance: "暂无" }]}
    onJudgment={onJudgment} onReminders={onReminders} latest={<section aria-label="最新提醒" />} />);
  const chips = screen.getByRole("group", { name: "今日判断" });
  expect(within(chips).getAllByRole("button").map(button => button.getAttribute("aria-label"))).toEqual(["螺纹钢 今日判断", "铁矿石 暂无"]);
  fireEvent.click(within(chips).getByRole("button", { name: "铁矿石 暂无" }));
  expect(onJudgment).toHaveBeenCalledWith({ product: "DCE.i", name: "铁矿石", stance: "暂无" });
  fireEvent.click(within(screen.getByRole("group", { name: "今天概况" })).getByRole("button", { name: /^今天提醒/ }));
  expect(onReminders).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("region", { name: "最新提醒" }).compareDocumentPosition(screen.getByRole("group", { name: "时间线筛选" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(within(screen.getByRole("group", { name: "时间线筛选" })).getAllByRole("button").map(button => button.textContent)).toEqual(["全部", "成交", "AI 简报"]);
});

it("读取中的胶囊和暂无分开显示（读取中更淡），读到后只剩品种名（第 5 批不写立场）", () => {
  const { rerender } = render(<Home judgments={[{ product: "SHFE.ru", name: "橡胶", stance: "读取中" }]} />);
  const reading = within(screen.getByRole("group", { name: "今日判断" })).getByRole("button", { name: "橡胶 读取中" });
  expect(reading.querySelector("em")?.dataset.stance).toBe("loading");
  rerender(<Home judgments={[{ product: "SHFE.ru", name: "橡胶", stance: "偏多", symbol: "SHFE.ru2701" }]} />);
  expect(within(screen.getByRole("group", { name: "今日判断" })).getByRole("button", { name: "橡胶 今日判断" }).querySelector("em")).toBeNull();
});

it("收盘前持仓检查按合约的交易时段：日盘收盘前 5 分钟，夜盘到次日的也算，最后一段过了是下一个交易日的日盘", () => {
  const schedule = { day: [["09:00:00", "10:15:00"], ["13:30:00", "15:00:00"]] as [string, string][], night: [["21:00:00", "26:30:00"]] as [string, string][] };
  const beijing = (hour: number, minute = 0) => Date.UTC(2026, 9, 6, hour - 8, minute);
  expect(closeCheckTime(schedule, beijing(10))).toBe("14:55");
  expect(closeCheckTime(schedule, beijing(21, 30))).toBe("02:25");
  expect(closeCheckTime({ day: schedule.day, night: [] }, beijing(21, 30))).toBe("14:55");
  expect(closeCheckTime(null, beijing(10))).toBeNull();
});

it("置顶区只完整显示最近的一张待确认卡，其余折成一行“还有 N 张待确认”，点开就地展开、再点收起；只有一张时照旧完整显示", () => {
  const card = (id: string, minutes: number) => ({ kind: "proposal" as const, id, time: now - minutes * 60_000,
    card: { id, type: "trade" as const, payload: { proposalId: id, status: "approval_required" } } as never });
  const renderCard = (item: { id: string }) => <article aria-label={`方案卡 ${item.id}`}><button type="button">确认</button></article>;
  const view = render(<Home pending={[card("p3", 1), card("p2", 2), card("p1", 3)]} renderCard={renderCard} />);
  const cards = () => screen.queryAllByRole("article", { name: /^方案卡/ }).map(item => item.getAttribute("aria-label"));
  expect(cards()).toEqual(["方案卡 p3"]);
  const more = screen.getByRole("button", { name: "还有 2 张待确认" });
  expect(more).toHaveAttribute("aria-expanded", "false");
  expect(within(more).queryByRole("button", { name: "确认" })).toBeNull();
  fireEvent.click(more);
  expect(cards()).toEqual(["方案卡 p3", "方案卡 p2", "方案卡 p1"]);
  fireEvent.click(screen.getByRole("button", { name: "收起其余待确认" }));
  expect(cards()).toEqual(["方案卡 p3"]);
  expect(within(screen.getByRole("group", { name: "今天概况" })).getByRole("button", { name: /^待确认/ })).toHaveTextContent("3 张");
  expect(within(screen.getByRole("group", { name: "今天概况" })).getByRole("button", { name: /^待确认/ })).toHaveAttribute("aria-label", "待确认 3 张，还剩 9 分钟");
  view.rerender(<Home pending={[card("p3", 1)]} renderCard={renderCard} />);
  expect(cards()).toEqual(["方案卡 p3"]);
  expect(screen.queryByRole("button", { name: /张待确认$/ })).toBeNull();
});


it("本站模拟两本账：持仓格里美元的浮动盈亏单独一行，不和人民币相加（2026-10-22）", () => {
  render(<Home today={{ positions: 2, floating: -610, floatingUsd: 160, reminders: 0, unreadReminders: 0 }} />);
  const tile = within(screen.getByRole("group", { name: "今天概况" })).getByRole("button", { name: /^持仓/ });
  expect(tile).toHaveTextContent("浮亏 -610");
  expect(tile).toHaveTextContent("美元 +160");
  expect(tile).toHaveAttribute("aria-label", "持仓 2 笔，浮亏 -610，外盘 +160 美元");
});
