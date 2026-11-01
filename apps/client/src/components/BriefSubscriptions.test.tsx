import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { vi } from "vitest";
import type { BriefSubscriptions, InstrumentCatalogItem } from "../api";
import { readQueryClient, resetReadIdentity } from "../runtimeClient";
import { applyUiSurface } from "../uiSurfaces";
import { BriefSubscriptionEditor } from "./BriefSubscriptions";
import { MobileBriefs } from "./mobile/MobileBriefs";
import { MobileMe } from "./mobile/MobileMe";

const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={readQueryClient}>{children}</QueryClientProvider>;
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const contract = (exchangeId: string, productId: string, name: string): InstrumentCatalogItem => ({
  symbol: `${exchangeId}.${productId}2601`, exchangeId, instrumentId: `${productId}2601`, instrumentName: `${name}2601`, productId,
  priceTick: 1, volumeMultiple: 10, deliveryYear: 2026, deliveryMonth: 1, expireRestDays: 90, hasNight: true, isMain: true,
});
const instruments = [contract("SHFE", "rb", "螺纹钢"), contract("DCE", "i", "铁矿石"), contract("SHFE", "au", "黄金"), contract("CZCE", "MA", "甲醇")];
const product = (key: string, name: string) => ({ product: key, name, symbol: `${key}2601` });

/** A brief-subscriptions Runtime: GET returns `state`, PUT stores the list (or answers `failWith`). */
function runtime(initial: BriefSubscriptions) {
  let state = initial;
  const puts: string[][] = [];
  let failWith: [number, unknown] | undefined;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (!url.endsWith("/api/app/brief-subscriptions")) return json({ detail: "not found" }, 404);
    if (init?.method !== "PUT") return json(state);
    const products = (JSON.parse(String(init.body)) as { products: string[] }).products;
    puts.push(products);
    if (failWith) { const [status, body] = failWith; failWith = undefined; return json(body, status); }
    const names = Object.fromEntries(instruments.map(item => [`${item.exchangeId}.${item.productId}`, item.instrumentName.replace(/\d+$/, "")]));
    state = { products: products.map(key => product(key, names[key] ?? key)), limit: state.limit, source: "user" };
    return json(state);
  }));
  return { puts, failNext: (status: number, body: unknown) => { failWith = [status, body]; } };
}

const editor = (watchlist: string[] = []) => render(
  <BriefSubscriptionEditor baseUrl="http://runtime" instruments={instruments} watchlist={watchlist} />, { wrapper });
const names = () => within(screen.getByRole("region", { name: "订阅品种" })).queryAllByRole("button", { name: /^取消订阅/ })
  .map(button => button.getAttribute("aria-label")!.replace("取消订阅", ""));

beforeEach(() => { resetReadIdentity(); });
afterEach(() => { cleanup(); readQueryClient.clear(); vi.unstubAllGlobals(); });

it("shows the default list with its one-line note, and a user list without it", async () => {
  runtime({ products: [product("SHFE.rb", "螺纹钢")], limit: 3, source: "default" });
  editor();
  await waitFor(() => expect(names()).toEqual(["螺纹钢"]));
  expect(screen.getByRole("region", { name: "订阅品种" })).toHaveTextContent("默认使用你的自选品种");
  expect(screen.getByText("已选 1 / 3")).toBeVisible();
  cleanup(); readQueryClient.clear();
  runtime({ products: [product("SHFE.rb", "螺纹钢")], limit: null, source: "user" });
  editor();
  await waitFor(() => expect(names()).toEqual(["螺纹钢"]));
  expect(screen.getByRole("region", { name: "订阅品种" })).not.toHaveTextContent("默认使用你的自选品种");
  expect(screen.getByText("已选 1（不限）")).toBeVisible();
});

it("adds from the catalog (starred first) until the tier's cap, then disables 添加品种 and says why", async () => {
  const server = runtime({ products: [product("SHFE.rb", "螺纹钢")], limit: 3, source: "default" });
  editor(["SHFE.au"]);
  fireEvent.click(await screen.findByRole("button", { name: "添加品种" }));
  const picker = screen.getByRole("region", { name: "添加品种" });
  // Without a query the starred products not yet subscribed are offered.
  expect(within(picker).getAllByRole("button", { name: /^添加/ }).map(button => button.getAttribute("aria-label"))).toEqual(["添加黄金"]);
  fireEvent.click(within(picker).getByRole("button", { name: "添加黄金" }));
  await waitFor(() => expect(server.puts.at(-1)).toEqual(["SHFE.rb", "SHFE.au"]));
  await waitFor(() => expect(names()).toEqual(["螺纹钢", "黄金"]));
  fireEvent.change(within(picker).getByRole("textbox", { name: "搜索品种" }), { target: { value: "铁" } });
  fireEvent.click(within(picker).getByRole("button", { name: "添加铁矿石" }));
  await waitFor(() => expect(server.puts.at(-1)).toEqual(["SHFE.rb", "SHFE.au", "DCE.i"]));
  await waitFor(() => expect(screen.getByText("已选 3 / 3")).toBeVisible());
  expect(screen.getByText("当前档位最多 3 个")).toBeVisible();
  fireEvent.change(within(picker).getByRole("textbox", { name: "搜索品种" }), { target: { value: "ma" } });
  expect(within(picker).getByRole("button", { name: "添加甲醇" })).toBeDisabled();
  // Closing the picker at the cap leaves 添加品种 disabled.
  fireEvent.click(screen.getByRole("button", { name: "收起" }));
  expect(screen.getByRole("button", { name: "添加品种" })).toBeDisabled();
});

it("cancels and reorders with one PUT each, in push order", async () => {
  const server = runtime({ products: [product("SHFE.rb", "螺纹钢"), product("DCE.i", "铁矿石"), product("SHFE.au", "黄金")], limit: 10, source: "user" });
  editor();
  await waitFor(() => expect(names()).toEqual(["螺纹钢", "铁矿石", "黄金"]));
  expect(screen.getByRole("button", { name: "上移螺纹钢" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "下移黄金" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "上移黄金" }));
  await waitFor(() => expect(server.puts.at(-1)).toEqual(["SHFE.rb", "SHFE.au", "DCE.i"]));
  await waitFor(() => expect(names()).toEqual(["螺纹钢", "黄金", "铁矿石"]));
  fireEvent.click(screen.getByRole("button", { name: "取消订阅螺纹钢" }));
  await waitFor(() => expect(server.puts.at(-1)).toEqual(["SHFE.au", "DCE.i"]));
  await waitFor(() => expect(names()).toEqual(["黄金", "铁矿石"]));
  expect(server.puts).toHaveLength(2);
});

it("puts the saved list back with the reason when a save fails", async () => {
  const server = runtime({ products: [product("SHFE.rb", "螺纹钢"), product("DCE.i", "铁矿石")], limit: 3, source: "user" });
  editor();
  await waitFor(() => expect(names()).toEqual(["螺纹钢", "铁矿石"]));
  server.failNext(422, { detail: { code: "brief_limit_exceeded", limit: 1 } });
  fireEvent.click(screen.getByRole("button", { name: "下移螺纹钢" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("当前档位最多 1 个，未保存"));
  expect(names()).toEqual(["螺纹钢", "铁矿石"]);
  server.failNext(500, { detail: "boom" });
  fireEvent.click(screen.getByRole("button", { name: "取消订阅铁矿石" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("订阅未保存，请重试"));
  expect(names()).toEqual(["螺纹钢", "铁矿石"]);
});

it("over a lowered cap marks the unpushed products and holds the order until the list fits", async () => {
  const server = runtime({ products: [product("SHFE.rb", "螺纹钢"), product("DCE.i", "铁矿石"), product("SHFE.au", "黄金")], limit: 2, source: "user", overLimit: true });
  editor();
  await waitFor(() => expect(names()).toEqual(["螺纹钢", "铁矿石", "黄金"]));
  expect(screen.getByRole("status")).toHaveTextContent("超出当前档位上限，只推送前 2 个");
  expect(screen.getByText("SHFE.au2601 · 超出上限，未推送")).toBeVisible();
  expect(screen.getByRole("button", { name: "上移黄金" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "添加品种" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "取消订阅黄金" }));
  await waitFor(() => expect(server.puts.at(-1)).toEqual(["SHFE.rb", "DCE.i"]));
  await waitFor(() => expect(screen.getByRole("button", { name: "上移铁矿石" })).toBeEnabled());
  expect(screen.queryByText(/超出当前档位上限/)).toBeNull();
});

it("the picker is a commandable disclosure", async () => {
  runtime({ products: [], limit: 3, source: "user" });
  editor(["DCE.i"]);
  await screen.findByText("已选 0 / 3");
  expect(screen.getByText("还没有订阅品种；添加后 AI 会为它检索简报。")).toBeVisible();
  act(() => { applyUiSurface("disclosure:brief-add", true); });
  expect(within(screen.getByRole("region", { name: "添加品种" })).getByRole("button", { name: "添加铁矿石" })).toBeEnabled();
});

it("我的 → AI 简报推送 shows the count and opens its page with the push switch", async () => {
  const onOpenPage = vi.fn();
  const notify = { briefs: true, briefCount: 2 };
  const me = render(<MobileMe userName="测试" accountLabel="模拟" equity="1,000,000" alertCount={0} appearance="深色 · 标准"
    onOpenPage={onOpenPage} onOpenSettings={vi.fn()} onOpenAlerts={vi.fn()} notify={notify} onOpenReminderSettings={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: /AI 简报推送/ }));
  expect(screen.getByRole("button", { name: /AI 简报推送/ })).toHaveTextContent("已订阅 2 个");
  expect(onOpenPage).toHaveBeenCalledWith("briefs");
  me.rerender(<MobileMe userName="测试" accountLabel="模拟" equity="1,000,000" alertCount={0} appearance="深色 · 标准"
    onOpenPage={onOpenPage} onOpenSettings={vi.fn()} onOpenAlerts={vi.fn()} notify={{ ...notify, briefs: false }} onOpenReminderSettings={vi.fn()} />);
  expect(screen.getByRole("button", { name: /AI 简报推送/ })).toHaveTextContent("已关闭");
  me.unmount();
  runtime({ products: [product("SHFE.rb", "螺纹钢")], limit: 3, source: "default" });
  const onToggle = vi.fn(); const onClose = vi.fn();
  render(<MobileBriefs baseUrl="http://runtime" enabled offline={false} onToggle={onToggle} instruments={instruments} watchlist={[]} onClose={onClose} />, { wrapper });
  fireEvent.click(screen.getByRole("switch", { name: "AI 简报推送" }));
  expect(onToggle).toHaveBeenCalledWith(false);
  await waitFor(() => expect(names()).toEqual(["螺纹钢"]));
  fireEvent.click(screen.getByRole("button", { name: "返回我的" }));
  expect(onClose).toHaveBeenCalled();
});
