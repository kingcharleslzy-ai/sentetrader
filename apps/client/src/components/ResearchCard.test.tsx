import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ResearchCard } from "./ResearchCard";
import { resetUiSurfaces, uiSurface } from "../uiSurfaces";

const summary = {
  symbol: "SHFE.rb2610", product: "rb", contract: "rb2610", generatedAt: "2026-09-13T12:00:00+00:00",
  datasets: {
    position_rank: { dataset: "position_rank", status: "ok", asOf: "20260911", source: "上期所", data: { netLong: -1200, longTotal: 30000, shortTotal: 31200, longChange: 500, shortChange: -300 } },
    warehouse_receipt: { dataset: "warehouse_receipt", status: "unavailable", asOf: null, source: null, error: "research_source_error:JSONDecodeError", data: null },
    spot_basis: { dataset: "spot_basis", status: "stale", asOf: "20260910", source: "生意社", error: "research_fetch_timeout", data: { spotPrice: 3120, dominantBasis: 74, dominantBasisRate: 0.0237 } },
    inventory: { dataset: "inventory", status: "ok", asOf: "2026-09-05", source: "东方财富", data: { latest: { value: 512.3, date: "2026-09-05" }, trend: "down" } },
    term_structure: { dataset: "term_structure", status: "ok", asOf: "20260911", source: "各月结算价", data: { shape: "backwardation", nearContract: "RB2609", farContract: "RB2705", spread: -40 } },
    fees_margin: { dataset: "fees_margin", status: "ok", asOf: "2026-09-11", source: "九期网", data: { contract: "rb2610", marginRateLong: 13, feePerLot: 6.1 } }
  }
};

afterEach(() => { vi.unstubAllGlobals(); resetUiSurfaces(); });

test("研究数据卡只列出取到的数据集、标记旧值，收起后不留正文", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(summary), { headers: { "Content-Type": "application/json" } })));
  render(<ResearchCard baseUrl="https://runtime.test" symbol="SHFE.rb2610" replay={false} />);
  const card = await screen.findByTestId("research-card");
  await waitFor(() => expect(card).toHaveTextContent("RB · 4 项，1 项为旧值"));
  expect(uiSurface("monitorCard:research")?.state).toBe("expanded");
  const rows = within(card).getAllByRole("listitem");
  // The unavailable warehouse receipt is not a row and nothing says 暂缺.
  // The inventory dataset repeats the receipts and is not listed.
  expect(rows.map(row => row.getAttribute("data-dataset"))).toEqual(["spot_basis", "position_rank", "term_structure", "fees_margin"]);
  expect(card).not.toHaveTextContent("暂缺");
  expect(card).not.toHaveTextContent("/6");
  expect(rows[0]).toHaveTextContent("基差 +74（2.37%）"); expect(rows[0]).toHaveTextContent("旧值");
  expect(rows[1]).toHaveTextContent("前20净持仓 -1,200");
  expect(rows[2]).toHaveTextContent("远月贴水 backwardation");
  expect(rows[3]).toHaveTextContent("保证金 13% · 手续费 6.1 元/手");
  within(card).getByRole("button", { name: "收起" }).click();
  await waitFor(() => expect(within(card).queryByRole("list")).toBeNull());
  expect(card.querySelector(".card-body")).toBeNull();
  within(card).getByRole("button", { name: "展开" }).click();
  await waitFor(() => expect(within(card).getByRole("list")).toBeVisible());
});

test("没有任何可用数据集（本机模式缺依赖）时整卡不渲染；回放不显示", async () => {
  const missing = { ...summary, datasets: Object.fromEntries(Object.keys(summary.datasets).map(key => [key, { dataset: key, status: "unavailable", asOf: null, source: null, error: "research_dependency_missing", data: null }])) };
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(missing), { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  const view = render(<ResearchCard baseUrl="https://runtime.test" symbol="SHFE.rb2610" replay={false} />);
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  await new Promise(resolve => setTimeout(resolve, 20));
  expect(screen.getByRole("alert")).toHaveTextContent("研究数据暂不可用");
  expect(screen.queryByText("research_dependency_missing")).toBeNull();
  // No card, no surface: `monitorCards` must not report a research card nobody can see.
  expect(uiSurface("monitorCard:research")).toBeUndefined();
  view.rerender(<ResearchCard baseUrl="https://runtime.test" symbol="SHFE.rb2610" replay />);
  expect(screen.queryByTestId("research-card")).toBeNull();
});

test("切品种等待新请求时不显示上个品种的源错误", async () => {
  const missing = { ...summary, datasets: {inventory: {dataset:"inventory", status:"unavailable", error:"old_product_error", data:null}} };
  vi.stubGlobal("fetch", vi.fn((url: string) => url.includes("rb2610") ? Promise.resolve(new Response(JSON.stringify(missing))) : new Promise<Response>(() => {})));
  const view = render(<ResearchCard baseUrl="https://runtime.test" symbol="SHFE.rb2610" replay={false} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("研究数据暂不可用");
  expect(screen.queryByText(/old_product_error/)).toBeNull();
  view.rerender(<ResearchCard baseUrl="https://runtime.test" symbol="DCE.i2701" replay={false} />);
  expect(screen.getByRole("status")).toHaveTextContent("加载中");
  expect(screen.queryByText(/old_product_error/)).toBeNull();
});
