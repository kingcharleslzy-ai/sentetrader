import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, test, vi } from "vitest";

import { requestJson } from "../../api";
import { readQueryClient } from "../../runtimeClient";
import { resetUiSurfaces, uiSurface } from "../../uiSurfaces";
import { MemoryAdvancedSettings, MemorySettings } from "./MemorySettings";
import type { PreferencesHandle } from "../../userPreferences";

vi.mock("../../api", () => ({ requestJson: vi.fn() }));

const AT = "2026-09-29T09:00:00Z"; // mid-day everywhere a test runner lives: the label reads 9月29日.

const document = {
  enabled: true,
  items: [
    { id: "m1", layer: "profile", layerLabel: "画像", text: "只做螺纹", source: "user",
      evidence: { via: "chat", quote: "记住我只做螺纹", at: AT }, tags: [], status: "active",
      createdAt: AT, updatedAt: AT, invalidAt: null, invalidReason: null },
    { id: "m2", layer: "rule", layerLabel: "约定", text: "给建议先说止损位", source: "user",
      evidence: { via: "settings", quote: null, at: AT }, tags: [], status: "active",
      createdAt: AT, updatedAt: AT, invalidAt: null, invalidReason: null },
    { id: "m3", layer: "lesson", layerLabel: "经验", text: "追高吃亏", source: "stats",
      evidence: { via: "consolidation", n: 14, at: AT }, tags: ["铁矿"], status: "active",
      createdAt: AT, updatedAt: AT, invalidAt: null, invalidReason: null },
    { id: "m4", layer: "profile", layerLabel: "画像", text: "不做夜盘", source: "user",
      evidence: { via: "consolidation", quote: "我不做夜盘，尽量白天搞定", at: AT }, tags: [],
      status: "active", createdAt: AT, updatedAt: AT, invalidAt: null, invalidReason: null },
  ],
  changes: [
    { id: "c1", memoryId: "m1", op: "add", actor: "ai", at: AT, before: null, after: null },
    { id: "c2", memoryId: "m2", op: "invalidate", actor: "user", at: AT, before: null, after: null },
    { id: "c3", memoryId: "m1", op: "invalidate", actor: "ai", at: AT, before: null, after: null },
    { id: "c4", memoryId: "m3", op: "add", actor: "consolidation", at: AT, before: null, after: null },
  ],
};

const preferences = { preferences: { chat: { replyStyle: "concise", autoReadScreen: true, memory: true, sendKey: "enter" } } };
const handle = preferences as unknown as PreferencesHandle;
const setPref = vi.fn();

function mocked(error?: unknown) {
  vi.mocked(requestJson).mockImplementation((url: string, init?: RequestInit) => {
    if (error && init?.method) return Promise.reject(error);
    return Promise.resolve(document);
  });
}

function renderSection() {
  render(<MemorySettings baseUrl="https://runtime.test" preferences={handle} setPref={setPref} />);
}

/** 约定、经验、添加一条与最近变动 moved to the memory-advanced second-level page (设置拆页 2026-09-30). */
function renderAdvanced() {
  render(<MemoryAdvancedSettings baseUrl="https://runtime.test" preferences={handle} />);
}

beforeEach(() => {
  vi.resetAllMocks();
  readQueryClient.clear();
  resetUiSurfaces();
  mocked();
});

test("groups the memory by layer with each item's evidence line", async () => {
  renderSection();
  expect(await screen.findByText("只做螺纹")).toBeVisible();
  expect(screen.getByText("不做夜盘")).toBeVisible();
  expect(screen.getByText("你说：「记住我只做螺纹」· 9月29日")).toBeVisible();
  expect(screen.getByText("整理自你说的：「我不做夜盘，尽量白天搞定」· 9月29日")).toBeVisible();
  expect(screen.queryByText("给建议先说止损位")).toBeNull();
  renderAdvanced();
  expect(await screen.findByText("给建议先说止损位")).toBeVisible();
  expect(screen.getByText("追高吃亏")).toBeVisible();
  expect(screen.getByText("设置里填写 · 9月29日")).toBeVisible();
  expect(screen.getByText("交易统计 · 近 90 天 14 笔")).toBeVisible();
  expect(screen.getByText("AI 记下")).toBeVisible();
  expect(screen.getByText("你删除")).toBeVisible();
  expect(screen.getByText("AI 删除")).toBeVisible();
  expect(screen.getByText("自动整理：记下")).toBeVisible();
  // The surface registers in an effect after the list renders; slow CI hosts can lag a frame behind.
  await waitFor(() => expect(uiSurface("memory")?.state).toEqual({ enabled: true, counts: { profile: 2, rule: 1, lesson: 1 } }));
});

test("the consolidate button posts, reports its result and shows a 429's detail", async () => {
  renderAdvanced();
  await screen.findByText("给建议先说止损位");
  const reply = (body: unknown) => vi.mocked(requestJson).mockImplementation((url: string, init?: RequestInit) => {
    if (init?.method === "POST" && String(url).endsWith("/consolidate")) return Promise.resolve(body);
    return Promise.resolve(document);
  });
  reply({ stats: { added: 2, updated: 1, invalidated: 0 }, words: { applied: 1, ops: { add: 1, update: 0, invalidate: 0 } } });
  fireEvent.click(screen.getByRole("button", { name: "现在整理一次" }));
  await waitFor(() => expect(requestJson).toHaveBeenCalledWith("https://runtime.test/api/memory/consolidate",
    expect.objectContaining({ method: "POST" })));
  expect(await screen.findByText("记下 3 条、更新 1 条、失效 0 条")).toBeVisible();

  reply(Promise.reject(Object.assign(new Error("429"), { status: 429, detail: { detail: "刚整理过，10 分钟后再试" } })));
  fireEvent.click(screen.getByRole("button", { name: "现在整理一次" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("刚整理过，10 分钟后再试");
});

test("adding posts to the memory route and reloads; a refusal shows its detail", async () => {
  renderAdvanced();
  await screen.findByText("给建议先说止损位");
  fireEvent.change(screen.getByLabelText("记忆内容"), { target: { value: "我不做夜盘" } });
  fireEvent.change(screen.getByLabelText("记忆层级"), { target: { value: "rule" } });
  fireEvent.click(screen.getByRole("button", { name: "添加" }));
  await waitFor(() => expect(requestJson).toHaveBeenCalledWith("https://runtime.test/api/memory",
    expect.objectContaining({ method: "POST" })));
  const posted = vi.mocked(requestJson).mock.calls.find(([, init]) => init?.method === "POST")!;
  expect(JSON.parse(String(posted[1]?.body))).toEqual({ layer: "rule", text: "我不做夜盘", via: "settings" });

  mocked(Object.assign(new Error("422"), { status: 422, detail: { detail: "一条记忆最多 120 字" } }));
  fireEvent.change(screen.getByLabelText("记忆内容"), { target: { value: "再记一条" } });
  fireEvent.click(screen.getByRole("button", { name: "添加" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("一条记忆最多 120 字");
});

test("editing an item saves with PUT", async () => {
  renderSection();
  await screen.findByText("只做螺纹");
  fireEvent.click(screen.getAllByRole("button", { name: "修改" })[0]);
  fireEvent.change(screen.getByLabelText("修改画像"), { target: { value: "只做螺纹和铁矿" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(requestJson).toHaveBeenCalledWith("https://runtime.test/api/memory/m1",
    expect.objectContaining({ method: "PUT", body: JSON.stringify({ text: "只做螺纹和铁矿" }) })));
});

test("a failed save keeps the editor open with the draft", async () => {
  renderSection();
  await screen.findByText("只做螺纹");
  mocked(Object.assign(new Error("422"), { status: 422, detail: { detail: "一条记忆最多 120 字" } }));
  fireEvent.click(screen.getAllByRole("button", { name: "修改" })[0]);
  fireEvent.change(screen.getByLabelText("修改画像"), { target: { value: "只做螺纹和铁矿" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("一条记忆最多 120 字");
  expect(screen.getByLabelText("修改画像")).toHaveValue("只做螺纹和铁矿");
});

test("a double-clicked 添加 stores once and keeps a draft typed while it was saving", async () => {
  let landPost: (value: unknown) => void = () => undefined;
  vi.mocked(requestJson).mockImplementation((url: string, init?: RequestInit) =>
    init?.method === "POST" ? new Promise(resolve => { landPost = resolve; }) : Promise.resolve(document));
  renderAdvanced();
  await screen.findByText("给建议先说止损位");
  fireEvent.change(screen.getByLabelText("记忆内容"), { target: { value: "我不做夜盘" } });
  fireEvent.click(screen.getByRole("button", { name: "添加" }));
  fireEvent.click(screen.getByRole("button", { name: "添加" }));
  fireEvent.change(screen.getByLabelText("记忆内容"), { target: { value: "下一条草稿" } });
  landPost({});
  await waitFor(() => expect(screen.getByRole("button", { name: "添加" })).toBeEnabled());
  expect(vi.mocked(requestJson).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  expect(screen.getByLabelText("记忆内容")).toHaveValue("下一条草稿");
});

test("an older read finishing late does not bring back a deleted item", async () => {
  const reads: ((value: unknown) => void)[] = [];
  vi.mocked(requestJson).mockImplementation((url: string, init?: RequestInit) =>
    init?.method ? Promise.resolve({}) : new Promise(resolve => { reads.push(resolve); }));
  renderSection();
  await waitFor(() => expect(reads).toHaveLength(1));
  reads[0]({ ...document });
  await screen.findByText("只做螺纹");
  // A reload is in flight (e.g. after another write) when the user deletes m1.
  vi.mocked(requestJson).mockClear();
  fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
  await waitFor(() => expect(reads).toHaveLength(2));
  const stale = reads.length;
  fireEvent.click(screen.getAllByRole("button", { name: "删除" })[1]);
  await waitFor(() => expect(reads).toHaveLength(stale + 1));
  reads[stale]({ ...document, items: document.items.filter(item => item.id !== "m1" && item.id !== "m4") });
  await waitFor(() => expect(screen.queryByText("只做螺纹")).toBeNull());
  reads[stale - 1]({ ...document, items: document.items.filter(item => item.id !== "m1") });
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(screen.queryByText("只做螺纹")).toBeNull();
  expect(screen.queryByText("不做夜盘")).toBeNull();
});

test("deleting an item calls DELETE on its id", async () => {
  renderSection();
  await screen.findByText("只做螺纹");
  fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
  await waitFor(() => expect(requestJson).toHaveBeenCalledWith("https://runtime.test/api/memory/m1",
    expect.objectContaining({ method: "DELETE" })));
});

test("a change row's 撤销 posts the undo route", async () => {
  renderAdvanced();
  await screen.findByText("AI 记下");
  fireEvent.click(screen.getAllByRole("button", { name: "撤销" })[0]);
  await waitFor(() => expect(requestJson).toHaveBeenCalledWith("https://runtime.test/api/memory/changes/c1/undo",
    expect.objectContaining({ method: "POST" })));
});

test("the toggle writes the chat.memory preference", async () => {
  renderSection();
  await screen.findByText("只做螺纹");
  fireEvent.click(screen.getByRole("switch", { name: "让 AI 记住我" }));
  expect(setPref).toHaveBeenCalledWith("chat.memory", false);
});

test("without a Runtime the section is read-only and reports the memory switch only", () => {
  render(<MemorySettings baseUrl={undefined} preferences={handle} setPref={setPref} />);
  expect(screen.getByText("让 AI 记住我")).toBeVisible();
  expect(screen.getByText("未连接 Runtime，只读")).toBeVisible();
  expect(screen.queryByLabelText("记忆内容")).not.toBeInTheDocument();
  expect(uiSurface("memory")?.state).toEqual({ enabled: true, counts: { profile: 0, rule: 0, lesson: 0 } });
});

test("a failed first read says so instead of 还没有, and 重试 reads again", async () => {
  vi.mocked(requestJson).mockRejectedValueOnce(Object.assign(new Error("502"), { status: 502 }));
  renderSection();
  expect(await screen.findByRole("alert")).toHaveTextContent("记忆没有读到");
  expect(screen.queryByText("还没有")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByText("只做螺纹")).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a failed refresh after a write keeps the list it had", async () => {
  renderSection();
  await screen.findByText("只做螺纹");
  vi.mocked(requestJson).mockImplementation((url: string, init?: RequestInit) =>
    init?.method ? Promise.resolve({}) : Promise.reject(Object.assign(new Error("502"), { status: 502 })));
  fireEvent.click(screen.getAllByRole("button", { name: "删除" })[1]);
  expect(await screen.findByRole("alert")).toHaveTextContent("刷新没有成功，显示的是上次读到的内容");
  expect(screen.getByText("只做螺纹")).toBeVisible();
  expect(screen.getByText("不做夜盘")).toBeVisible();
});

test("a save in flight blocks a second save and keeps a draft changed meanwhile", async () => {
  let landPut: (value: unknown) => void = () => undefined;
  vi.mocked(requestJson).mockImplementation((url: string, init?: RequestInit) =>
    init?.method === "PUT" ? new Promise(resolve => { landPut = resolve; }) : Promise.resolve(document));
  renderSection();
  await screen.findByText("只做螺纹");
  fireEvent.click(screen.getAllByRole("button", { name: "修改" })[0]);
  fireEvent.change(screen.getByLabelText("修改画像"), { target: { value: "只做螺纹和铁矿" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  fireEvent.change(screen.getByLabelText("修改画像"), { target: { value: "只做螺纹、铁矿和焦炭" } });
  landPut({});
  await waitFor(() => expect(screen.getByRole("button", { name: "保存" })).toBeEnabled());
  expect(vi.mocked(requestJson).mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
  expect(screen.getByLabelText("修改画像")).toHaveValue("只做螺纹、铁矿和焦炭");
});

test("an add that lands keeps a draft whose layer changed meanwhile", async () => {
  let landPost: (value: unknown) => void = () => undefined;
  vi.mocked(requestJson).mockImplementation((url: string, init?: RequestInit) =>
    init?.method === "POST" ? new Promise(resolve => { landPost = resolve; }) : Promise.resolve(document));
  renderAdvanced();
  await screen.findByText("给建议先说止损位");
  fireEvent.change(screen.getByLabelText("记忆内容"), { target: { value: "不做夜盘" } });
  fireEvent.click(screen.getByRole("button", { name: "添加" }));
  // Same text, another layer: the user is preparing a second entry, not the one in flight.
  fireEvent.change(screen.getByLabelText("记忆层级"), { target: { value: "rule" } });
  landPost({});
  await waitFor(() => expect(screen.getByRole("button", { name: "添加" })).toBeEnabled());
  expect(screen.getByLabelText("记忆内容")).toHaveValue("不做夜盘");
});

test("整理 says which part did not finish instead of 没有新东西要记", async () => {
  renderAdvanced();
  await screen.findByText("给建议先说止损位");
  const reply = (body: unknown) => vi.mocked(requestJson).mockImplementation((url: string, init?: RequestInit) =>
    init?.method === "POST" && String(url).endsWith("/consolidate") ? Promise.resolve(body) : Promise.resolve(document));
  reply({ stats: { added: 0, updated: 0, invalidated: 0, skipped: 2 }, words: { error: "TimeoutError" } });
  fireEvent.click(screen.getByRole("button", { name: "现在整理一次" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("从你说的话里提炼没有完成，稍后再试");
  expect(screen.queryByText("没有新东西要记")).toBeNull();

  reply({ stats: { added: 1, updated: 0, invalidated: 0, skipped: 0 }, words: { skipped: "cancelled" } });
  fireEvent.click(screen.getByRole("button", { name: "现在整理一次" }));
  expect(await screen.findByText("记下 1 条、更新 0 条、失效 0 条")).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("这次整理被新的一次接手");

  reply({ stats: { added: 0, updated: 0, invalidated: 0, skipped: 0 }, words: { skipped: "no_words" } });
  fireEvent.click(screen.getByRole("button", { name: "现在整理一次" }));
  expect(await screen.findByText("没有新东西要记")).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("整理 on the advanced page refreshes the 画像 the main page shows", async () => {
  renderSection();
  renderAdvanced();
  await screen.findByText("给建议先说止损位");
  const added = { ...document.items[0], id: "m9", text: "偏好日内" };
  vi.mocked(requestJson).mockClear();
  vi.mocked(requestJson).mockImplementation((url: string, init?: RequestInit) =>
    init?.method === "POST" ? Promise.resolve({ stats: { added: 1 }, words: { skipped: "no_words" } })
      : Promise.resolve({ ...document, items: [...document.items, added] }));
  fireEvent.click(screen.getByRole("button", { name: "现在整理一次" }));
  expect(await screen.findByText("偏好日内")).toBeVisible();
  // One shared read for both pages, not one per page.
  expect(vi.mocked(requestJson).mock.calls.filter(([, init]) => !init?.method)).toHaveLength(1);
});
