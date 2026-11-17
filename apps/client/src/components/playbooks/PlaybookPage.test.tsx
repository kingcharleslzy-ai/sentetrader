import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PlaybookPage } from "./PlaybookPage";
import { readQueryClient, resetReadIdentity } from "../../runtimeClient";

const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
const note = { id: "p1", name: "第一份", entryRules: "", exitRules: "", stopRules: "", strategyId: null };
afterEach(() => { vi.unstubAllGlobals(); readQueryClient.clear(); });

it("a successful save starts a new list read even while the first list is pending", async () => {
  let first!: (value: Response) => void;
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    if (!String(url).includes("/playbooks")) return Promise.resolve(json({ items: [] }));
    if (init?.method === "POST") return Promise.resolve(json({ playbook: note }));
    if (++reads === 1) return new Promise<Response>(resolve => { first = resolve; });
    return Promise.resolve(json({ playbooks: [note], max: 20 }));
  }));
  render(<PlaybookPage baseUrl="http://runtime" />);
  fireEvent.click(screen.getByRole("button", { name: "新建笔记" }));
  fireEvent.change(screen.getByRole("textbox", { name: "笔记名字" }), { target: { value: note.name } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByTestId("playbook:p1");
  await act(async () => { first(json({ playbooks: [], max: 20 })); });
  expect(screen.getByTestId("playbook:p1")).toBeVisible();
  expect(reads).toBe(2);
});

it("a previous login's late save cannot close the current editor", async () => {
  let save!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) => init?.method === "POST"
    ? new Promise<Response>(resolve => { save = resolve; }) : Promise.resolve(json({ playbooks: [], items: [], max: 20 }))));
  render(<PlaybookPage baseUrl="http://runtime" />);
  fireEvent.click(screen.getByRole("button", { name: "新建笔记" }));
  fireEvent.change(screen.getByRole("textbox", { name: "笔记名字" }), { target: { value: note.name } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  act(() => { resetReadIdentity(); });
  fireEvent.click(screen.getByRole("button", { name: "新建笔记" }));
  fireEvent.change(screen.getByRole("textbox", { name: "笔记名字" }), { target: { value: "新登录的笔记" } });
  await act(async () => { save(json({ playbook: note })); });
  expect(screen.getByRole("textbox", { name: "笔记名字" })).toBeVisible();
});
