import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { ConversationTurn } from "../../state/conversation";
import { AiCommandBar } from "./AiCommandBar";
import { AiCommandBubble, useAiCommand } from "./useAiCommand";

const task = (status: "running" | "completed") => ({ id: "task-1", type: "task" as const, payload: {
  turnId: "a1", taskId: "task-1", title: "处理", subtitle: "", progress: 100, status, steps: [
    { id: "s1", label: "调用软件功能", tool: "app_call", status: "done" as const, detail: "已完成",
      action: { text: "建了到价提醒：螺纹2701 涨到 3036 提醒", undo: { method: "DELETE" as const, path: "/api/market/alerts/al-1" } } },
    { id: "s2", label: "调用软件功能", tool: "app_call", status: "done" as const, detail: "已完成", action: { text: "打开调研面板", undo: null } },
    { id: "s3", label: "读取行情", tool: "get_market_snapshot", status: "done" as const, detail: "已完成" },
  ] } });

function Bubble({ turns, onSend, onOpenChat }: { turns: ConversationTurn[]; onSend: () => Promise<void>; onOpenChat: () => void }) {
  const command = useAiCommand({ baseUrl: "https://test", turns, busy: false, blockedReason: null, onSend });
  return <><button type="button" onClick={() => command.send("突破 3036 提醒我，打开调研")}>发</button><AiCommandBubble command={command} onOpenChat={onOpenChat} /></>;
}

test("AI 指令栏：回车发出、Shift+回车不发；结果在 AI 球旁：AI 做的事一句话，撤销走同一个删除接口；有确认卡时提示去对话里确认", async () => {
  const fetcher = vi.fn(async () => new Response("{}"));
  vi.stubGlobal("fetch", fetcher);
  const send = vi.fn(() => true);
  render(<AiCommandBar busy={false} blockedReason={null} send={send} chatOpen={false} />);
  const input = screen.getByRole("textbox", { name: "问 AI，或说一句要做的事" });
  fireEvent.change(input, { target: { value: "突破 3036 提醒我，打开调研" } });
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect(send).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: "Enter" });
  expect(send).toHaveBeenCalledWith("突破 3036 提醒我，打开调研", false);
  expect(input).toHaveValue("");

  const onSend = vi.fn(async () => undefined);
  const onOpenChat = vi.fn();
  const view = render(<Bubble turns={[]} onSend={onSend} onOpenChat={onOpenChat} />);
  fireEvent.click(screen.getByRole("button", { name: "发" }));
  expect(screen.getByRole("status")).toHaveTextContent("AI 正在处理…");

  const turns: ConversationTurn[] = [
    { id: "u1", role: "user", text: "突破 3036 提醒我，打开调研", timestamp: "", cards: [] },
    { id: "a1", role: "assistant", text: "好了，提醒已经建好。", timestamp: "", completed: true,
      cards: [task("completed"), { id: "p1", type: "trade", payload: {} as never }] },
  ];
  view.rerender(<Bubble turns={turns} onSend={onSend} onOpenChat={onOpenChat} />);
  expect(screen.getByText("建了到价提醒：螺纹2701 涨到 3036 提醒；打开调研面板")).toBeVisible();
  expect(screen.getAllByRole("button", { name: /撤销/ })).toHaveLength(1);
  expect(screen.getByText("有一张下单确认卡，确认后才会下单")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: /撤销/ }));
  await waitFor(() => expect(screen.getByText(/^已撤销：/)).toBeVisible());
  expect(fetcher).toHaveBeenCalledWith("https://test/api/market/alerts/al-1", expect.objectContaining({ method: "DELETE" }));

  fireEvent.click(screen.getByRole("button", { name: "在对话里确认" }));
  expect(onOpenChat).toHaveBeenCalled();
  expect(screen.queryByRole("region", { name: "AI 指令结果" })).not.toBeInTheDocument();
});
