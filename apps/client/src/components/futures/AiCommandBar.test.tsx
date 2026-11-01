import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { ConversationTurn } from "../../state/conversation";
import { AiCommandBar } from "./AiCommandBar";

const task = (status: "running" | "completed") => ({ id: "task-1", type: "task" as const, payload: {
  turnId: "a1", taskId: "task-1", title: "处理", subtitle: "", progress: 100, status, steps: [
    { id: "s1", label: "调用软件功能", tool: "app_call", status: "done" as const, detail: "已完成",
      action: { text: "建了到价提醒：螺纹2701 涨到 3036 提醒", undo: { method: "DELETE" as const, path: "/api/market/alerts/al-1" } } },
    { id: "s2", label: "调用软件功能", tool: "app_call", status: "done" as const, detail: "已完成", action: { text: "打开调研面板", undo: null } },
    { id: "s3", label: "读取行情", tool: "get_market_snapshot", status: "done" as const, detail: "已完成" },
  ] } });

test("AI 指令栏：发出一句后列出 AI 做了几件事，能撤销的点撤销走同一个删除接口；有确认卡时提示去对话里确认", async () => {
  const fetcher = vi.fn(async () => new Response("{}"));
  vi.stubGlobal("fetch", fetcher);
  const onSend = vi.fn(async () => undefined);
  const onOpenChat = vi.fn();
  const props = { baseUrl: "https://test", busy: false, blockedReason: null, onSend, onOpenChat };
  const view = render(<AiCommandBar {...props} turns={[]} />);
  fireEvent.change(screen.getByRole("textbox", { name: "问 AI，或说一句要做的事" }), { target: { value: "突破 3036 提醒我，打开调研" } });
  await act(async () => { fireEvent.submit(screen.getByRole("search", { name: "AI 指令栏" })); });
  expect(onSend).toHaveBeenCalledWith("突破 3036 提醒我，打开调研", false);
  expect(screen.getByRole("status")).toHaveTextContent("AI 正在处理…");

  const turns: ConversationTurn[] = [
    { id: "u1", role: "user", text: "突破 3036 提醒我，打开调研", timestamp: "", cards: [] },
    { id: "a1", role: "assistant", text: "好了，提醒已经建好。", timestamp: "", completed: true,
      cards: [task("completed"), { id: "p1", type: "trade", payload: {} as never }] },
  ];
  view.rerender(<AiCommandBar {...props} turns={turns} />);
  expect(screen.getByText("AI 做了 2 件事")).toBeVisible();
  expect(screen.getByText("建了到价提醒：螺纹2701 涨到 3036 提醒")).toBeVisible();
  expect(screen.getByText("打开调研面板")).toBeVisible();
  expect(screen.getAllByRole("button", { name: /撤销/ })).toHaveLength(1);
  expect(screen.getByText("有一张确认卡，确认后才会下单。")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: /撤销/ }));
  await waitFor(() => expect(screen.getByText("已撤销")).toBeVisible());
  expect(fetcher).toHaveBeenCalledWith("https://test/api/market/alerts/al-1", expect.objectContaining({ method: "DELETE" }));

  fireEvent.click(screen.getByRole("button", { name: "到对话里确认" }));
  expect(onOpenChat).toHaveBeenCalled();
});
