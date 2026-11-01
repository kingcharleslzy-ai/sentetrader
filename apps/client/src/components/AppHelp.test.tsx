import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { requestJson } from "../api";
import { resetUiSurfaces, uiSurface } from "../uiSurfaces";
import { AppHelp, openAppHelp } from "./AppHelp";

vi.mock("../api", () => ({ requestJson: vi.fn() }));

const topic = (title: string, group: string, extra: Record<string, unknown> = {}) => ({
  title, group, summary: `${title}的一句话`, steps: [`${title}第一步`, `${title}第二步`, `${title}第三步`],
  faq: [{ q: `${title}问一`, a: `${title}答一` }, { q: `${title}问二`, a: `${title}答二` }],
  detail: [`${title}的完整说明`], ...extra,
});
const MANUAL = {
  groups: [{ id: "start", title: "快速上手" }, { id: "market", title: "看行情" }, { id: "admin", title: "管理员", ownerOnly: true },
    { id: "developer", title: "开发者参考", collapsed: true }],
  aliases: { workspace: "tour" },
  topics: {
    tour: topic("五分钟上手", "start", { tour: [{ target: "header", label: "顶栏" }] }),
    order: topic("下单面板", "market", { detail: ["五档只展示有报价的档位", "点五档价格只填限价，不下单"] }),
    chart: topic("看 K 线图", "market"),
    admin: topic("后台管理", "admin"),
    "ui-commands": topic("界面命令总表", "developer"),
  },
};

beforeEach(() => { resetUiSurfaces(); vi.mocked(requestJson).mockResolvedValue(MANUAL); });
afterEach(() => vi.mocked(requestJson).mockReset());

const card = () => screen.getByRole("dialog", { name: "使用说明", hidden: true });
const state = () => uiSurface("help")?.state as Record<string, unknown>;
async function open(topicId: string, owner = false) {
  render(<AppHelp baseUrl="http://runtime" isOwner={owner} />);
  act(() => openAppHelp(topicId));
  await screen.findByRole("article", { hidden: true });
  // The surface re-registers in an effect after the render that shows the topic.
  await waitFor(() => expect(state()?.topic).toBeTruthy());
}

test("分组导航：左侧按分组列出主题，右侧是标题、一句话、编号步骤、常见问题，完整说明默认折叠", async () => {
  await open("order");
  expect(requestJson).toHaveBeenCalledWith("http://runtime/api/app/help");
  const nav = within(card()).getByRole("navigation", { name: "说明目录", hidden: true });
  expect(within(nav).getAllByRole("heading", { hidden: true }).map(item => item.textContent)).toEqual(["快速上手", "看行情"]);
  expect(within(nav).getByRole("button", { name: "下单面板", hidden: true })).toHaveAttribute("aria-current", "page");
  // 开发者参考 is folded at the bottom; 管理员 is not shown to a member.
  expect(within(nav).getByRole("button", { name: "开发者参考", hidden: true })).toHaveAttribute("aria-expanded", "false");
  expect(within(nav).queryByRole("button", { name: "界面命令总表", hidden: true })).toBeNull();
  expect(within(card()).queryByText("管理员")).toBeNull();
  const article = within(card()).getByRole("article", { hidden: true });
  expect(within(article).getByRole("heading", { level: 3, hidden: true })).toHaveTextContent("下单面板");
  expect(within(article).getByText("下单面板的一句话")).toHaveClass("app-help-summary");
  expect(within(article).getAllByRole("listitem", { hidden: true }).map(item => item.textContent)).toEqual(["下单面板第一步", "下单面板第二步", "下单面板第三步"]);
  expect(within(article).getByRole("button", { name: "完整说明", hidden: true })).toHaveAttribute("aria-expanded", "false");
  expect(within(article).queryByText("五档只展示有报价的档位")).toBeNull();
  // Picking another topic in the navigation shows it.
  fireEvent.click(within(nav).getByRole("button", { name: "看 K 线图", hidden: true }));
  expect(within(card()).getByRole("article", { hidden: true })).toHaveAccessibleName("看 K 线图");
  expect(state()).toMatchObject({ open: true, topic: "chart", group: "market", query: "", detailOpen: false, faqOpen: [] });
});

test("常见问题逐条展开，完整说明点开 / 收起", async () => {
  await open("order");
  const article = within(card()).getByRole("article", { hidden: true });
  const question = within(article).getByRole("button", { name: "下单面板问二", hidden: true });
  expect(within(article).queryByText("下单面板答二")).toBeNull();
  fireEvent.click(question);
  expect(question).toHaveAttribute("aria-expanded", "true");
  expect(within(article).getByText("下单面板答二")).toBeInTheDocument();
  expect(within(article).queryByText("下单面板答一")).toBeNull();
  expect(state()).toMatchObject({ faqOpen: [1] });
  const detail = within(article).getByRole("button", { name: "完整说明", hidden: true });
  fireEvent.click(detail);
  expect(within(article).getByText("点五档价格只填限价，不下单")).toBeInTheDocument();
  expect(state()).toMatchObject({ detailOpen: true });
  fireEvent.click(detail);
  expect(within(article).queryByText("点五档价格只填限价，不下单")).toBeNull();
});

test("搜索同时匹配各层：只命中完整说明时切到该主题、自动展开完整说明并高亮", async () => {
  await open("tour");
  fireEvent.change(within(card()).getByRole("searchbox", { name: "搜索说明", hidden: true }), { target: { value: "点五档" } });
  const nav = within(card()).getByRole("navigation", { name: "说明目录", hidden: true });
  expect(within(nav).getAllByRole("button", { hidden: true }).map(item => item.textContent)).toEqual(["下单面板"]);
  const article = within(card()).getByRole("article", { hidden: true });
  expect(article).toHaveAccessibleName("下单面板");
  expect(within(article).getByRole("button", { name: "完整说明", hidden: true })).toHaveAttribute("aria-expanded", "true");
  expect(article.querySelector("mark")).toHaveTextContent("点五档");
  expect(state()).toMatchObject({ topic: "order", query: "点五档", matches: ["order"], detailOpen: true });
  // A hit in a FAQ answer opens that question; a miss says so.
  fireEvent.change(within(card()).getByRole("searchbox", { hidden: true }), { target: { value: "K 线图答二" } });
  expect(within(card()).getByRole("button", { name: "看 K 线图问二", hidden: true })).toHaveAttribute("aria-expanded", "true");
  fireEvent.change(within(card()).getByRole("searchbox", { hidden: true }), { target: { value: "不存在的词" } });
  expect(within(card()).getByText("没有找到“不存在的词”相关的说明")).toBeInTheDocument();
});

test("窄卡片的主题下拉：按分组列出主题，选中即打开", async () => {
  await open("tour");
  const picker = within(card()).getByRole("combobox", { name: "选择主题", hidden: true });
  expect([...picker.querySelectorAll("optgroup")].map(group => group.label)).toEqual(["快速上手", "看行情", "开发者参考"]);
  fireEvent.change(picker, { target: { value: "ui-commands" } });
  expect(within(card()).getByRole("article", { hidden: true })).toHaveAccessibleName("界面命令总表");
  // The folded 开发者参考 opens with its topic.
  expect(state()).toMatchObject({ topic: "ui-commands", group: "developer", developerOpen: true });
});

test("旧 id 与界面命令：别名打开对应主题，help 命令切主题、分组、搜索和折叠，管理员主题只给主账号", async () => {
  await open("workspace");
  expect(within(card()).getByRole("article", { hidden: true })).toHaveAccessibleName("五分钟上手");
  expect(state()).toMatchObject({ open: true, topic: "tour" });
  const apply = (value: unknown) => act(() => uiSurface("help")?.apply?.(value));
  apply({ group: "developer" });
  expect(state()).toMatchObject({ topic: "ui-commands", developerOpen: true });
  apply({ topic: "order", detail: true, faq: { index: 0, open: true } });
  expect(state()).toMatchObject({ topic: "order", detailOpen: true, faqOpen: [0] });
  apply({ query: "K 线" });
  expect(state()).toMatchObject({ topic: "chart", query: "K 线", matches: ["chart"] });
  expect(() => uiSurface("help")?.apply?.({ topic: "nope" })).toThrow("没有这个帮助主题");
  expect(() => uiSurface("help")?.apply?.({ topic: "admin" })).toThrow("该说明仅主账号可见");
  expect(() => uiSurface("help")?.apply?.({ topic: "order", faq: { index: 0, open: true } })).not.toThrow();
  // The card closes through the old dialog command path too.
  fireEvent.click(within(card()).getByRole("button", { name: "关闭说明", hidden: true }));
  await waitFor(() => expect(state()).toMatchObject({ open: false }));
});

test("主账号看得到管理员分组", async () => {
  await open("admin", true);
  expect(within(card()).getByRole("article", { hidden: true })).toHaveAccessibleName("后台管理");
});

test("读取失败的状态进快照，help: {retry: true} 与“重试”按钮同一操作；已读到或未连接时拒绝", async () => {
  vi.mocked(requestJson).mockRejectedValueOnce(new Error("503 unavailable"));
  render(<AppHelp baseUrl="http://runtime" />);
  act(() => openAppHelp("tour"));
  await screen.findByRole("alert", { hidden: true });
  await waitFor(() => expect(state()).toMatchObject({ open: true, loadStatus: "error", loadError: "说明读取失败，应用服务暂时不可用", canRetry: true }));
  act(() => { uiSurface("help")!.apply!({ retry: true }); });
  expect(await screen.findByRole("article", { hidden: true })).toHaveAccessibleName("五分钟上手");
  await waitFor(() => expect(state()).toMatchObject({ loadStatus: "ready", loadError: null, canRetry: false }));
  expect(() => uiSurface("help")!.apply!({ retry: true })).toThrow("说明已读到，不用重新读取");
  document.body.innerHTML = "";
  resetUiSurfaces();
  render(<AppHelp />);
  act(() => openAppHelp("tour"));
  await waitFor(() => expect(state()).toMatchObject({ loadStatus: "offline", canRetry: false }));
  expect(() => uiSurface("help")!.apply!({ retry: true })).toThrow("应用服务未连接，连接后可查看说明");
});

test("说明正在读取时 retry 被拒、不发第二次读取，界面上也没有重试按钮", async () => {
  vi.mocked(requestJson).mockReturnValueOnce(new Promise(() => undefined));
  render(<AppHelp baseUrl="http://runtime" />);
  act(() => openAppHelp("tour"));
  await waitFor(() => expect(state()).toMatchObject({ open: true, loadStatus: "loading", canRetry: false }));
  expect(() => uiSurface("help")!.apply!({ retry: true })).toThrow("说明正在读取，稍候再试");
  expect(screen.queryByRole("button", { name: "重试", hidden: true })).toBeNull();
  expect(vi.mocked(requestJson)).toHaveBeenCalledTimes(1);
});

test("读取超时显示原因和重试，重试读到后显示说明；没有连接时不假装在读取（#712 B1）", async () => {
  vi.mocked(requestJson).mockRejectedValueOnce(Object.assign(new Error("应用服务 30 秒内没有响应"), { status: 0, code: "request_timeout" }));
  render(<AppHelp baseUrl="http://runtime" />);
  act(() => openAppHelp("tour"));
  const alert = await screen.findByRole("alert", { hidden: true });
  expect(alert).toHaveTextContent("说明读取超时，应用服务暂时没有响应");
  expect(vi.mocked(requestJson)).toHaveBeenCalledWith("http://runtime/api/app/help");
  fireEvent.click(within(alert).getByRole("button", { name: "重试", hidden: true }));
  expect(await screen.findByRole("article", { hidden: true })).toHaveAccessibleName("五分钟上手");
  expect(vi.mocked(requestJson)).toHaveBeenCalledTimes(2);
  document.body.innerHTML = "";
  render(<AppHelp />);
  act(() => openAppHelp("tour"));
  expect(within(card()).getByText("应用服务未连接，连接后可查看说明")).toBeInTheDocument();
});
