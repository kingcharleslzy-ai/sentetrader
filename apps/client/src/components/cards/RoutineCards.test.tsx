import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import type { RoutineCard as Routine, RoutineRunCard as RoutineRun } from "@ai-trader/ui-events";
import { requestJson } from "../../api";
import { resetUiSurfaces, uiSurface } from "../../uiSurfaces";
import { RoutineSettings } from "../futures/RoutineSettings";
import { RoutineCard, RoutineRunCard } from "./RoutineCards";

vi.mock("../../api", () => ({ requestJson: vi.fn() }));
beforeEach(() => { vi.mocked(requestJson).mockReset(); resetUiSurfaces(); });

const draft: Routine = {
  turnId: "t1", cardId: "routine-rd-1", draftId: "rd-1", routineId: null, mode: "create", name: "老规矩", status: "draft",
  steps: [{ kind: "read", kindLabel: "读", text: "读自选的今日判断" }, { kind: "card", kindLabel: "出卡", text: "出开仓卡带止损" }],
  refusal: null, savedAt: null, note: "带「出卡」的步骤只出卡，下单仍要你点确认",
};

test("流程卡: 保存 is the user's own write, 改一下 only fills the composer, a refused card offers no 保存", async () => {
  const saveRoutine = vi.fn().mockRejectedValueOnce(new Error("最多 5 个流程，先删一个"));
  const fillPrompt = vi.fn();
  const { rerender } = render(<RoutineCard card={draft} handlers={{ saveRoutine, fillPrompt }} />);
  expect(screen.getByTestId("routine-card")).toHaveTextContent("出卡");
  fireEvent.click(screen.getByRole("button", { name: "改一下" }));
  expect(fillPrompt).toHaveBeenCalledWith("改一下「老规矩」：");
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(saveRoutine).toHaveBeenCalledWith("rd-1");
  expect(await screen.findByRole("alert")).toHaveTextContent("最多 5 个流程");
  rerender(<RoutineCard card={{ ...draft, status: "refused", refusal: "流程里不能放免确认下单" }} handlers={{ saveRoutine, fillPrompt }} />);
  expect(screen.queryByRole("button", { name: "保存" })).toBeNull();
  expect(screen.getByRole("note")).toHaveTextContent("不能放免确认下单");
  rerender(<RoutineCard card={{ ...draft, status: "saved", savedAt: "2026-10-06T01:00:00Z" }} handlers={{ saveRoutine, fillPrompt }} />);
  expect(screen.queryByRole("button", { name: "保存" })).toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent("已存进我的流程");
});

test("进度卡: a 需你确认 step points at its card, a failed step says the rest did not run", () => {
  const run: RoutineRun = {
    turnId: "t2", cardId: "routine-run-t2", routineId: "rt-1", name: "老规矩", status: "stopped", startedAt: "2026-10-06T01:00:00Z",
    finishedAt: "2026-10-06T01:00:12Z", elapsedSeconds: 12, confirmCount: 1,
    steps: [{ kind: "card", kindLabel: "出卡", text: "出开仓卡", status: "confirm", result: "出了 2 张开仓卡，等你确认" },
            { kind: "read", kindLabel: "读", text: "看账户", status: "failed", result: "数据源没有回应" },
            { kind: "alert", kindLabel: "提醒", text: "设到价提醒", status: "skipped", result: null }],
  };
  render(<RoutineRunCard card={run} />);
  const card = screen.getByTestId("routine-run-card");
  expect(card).toHaveTextContent("停在半路");
  expect(card).toHaveTextContent("需你确认 · 卡片在下面");
  expect(card).toHaveTextContent("数据源没有回应");
  expect(card).toHaveTextContent("失败 · 后面的步骤没有做");
  expect(screen.getByLabelText("没做")).toBeInTheDocument();
});

test("我的流程: 运行 hands the picked routine (id + name, #522) and 改 the name to the chat, 删 asks once more and the snapshot carries the list", async () => {
  const list = { items: [{ id: "rt-1", name: "老规矩", stepCount: 2, steps: draft.steps, createdAt: "", updatedAt: "", lastRunAt: null }],
    limits: { routines: 5, steps: 10, nameChars: 12 } };
  vi.mocked(requestJson).mockImplementation(async (_url, init) => (init?.method === "DELETE" ? { deleted: "rt-1" } : list) as never);
  const onRun = vi.fn();
  const onEdit = vi.fn();
  render(<RoutineSettings baseUrl="http://runtime" onRun={onRun} onEdit={onEdit} />);
  fireEvent.click(await screen.findByRole("button", { name: "运行「老规矩」" }));
  expect(onRun).toHaveBeenCalledWith({ id: "rt-1", name: "老规矩" });
  fireEvent.click(screen.getByRole("button", { name: "改「老规矩」" }));
  expect(onEdit).toHaveBeenCalledWith("老规矩");
  await waitFor(() => expect((uiSurface("routines")?.state as { count: number } | undefined)?.count).toBe(1));
  fireEvent.click(screen.getByRole("button", { name: "删「老规矩」" }));
  expect(vi.mocked(requestJson).mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "确认删除「老规矩」" }));
  await waitFor(() => expect(vi.mocked(requestJson)).toHaveBeenCalledWith("http://runtime/api/routines/rt-1", { method: "DELETE" }));
});

test("保护：流程卡与设置列表按原话标出“只给建议 / 需要时执行”，普通步骤不加标（10-06 卡上说会设、实际不设）", async () => {
  const steps: Routine["steps"] = [
    { kind: "alert", kindLabel: "提醒", text: "看看要不要给螺纹设个到价提醒", manner: "advisory" },
    { kind: "alert", kindLabel: "提醒", text: "看看要不要设夜盘提醒，需要就设", manner: "conditional" },
    { kind: "alert", kindLabel: "提醒", text: "在 3127 设上方提醒" },
  ];
  render(<RoutineCard card={{ ...draft, steps }} handlers={{}} />);
  const items = screen.getByTestId("routine-card").querySelectorAll(".rt-steps > li");
  expect([...items].map(item => item.querySelector(".rt-manner")?.textContent ?? null)).toEqual(["只给建议", "需要时执行", null]);
  vi.mocked(requestJson).mockResolvedValueOnce({ items: [{ id: "rt-2", name: "收盘检查", steps, stepCount: 3, createdAt: "", updatedAt: "", lastRunAt: null }],
    limits: { routines: 5, steps: 10, nameChars: 12 } });
  render(<RoutineSettings baseUrl="http://runtime" onRun={vi.fn()} onEdit={vi.fn()} />);
  expect(await screen.findByText(/看看要不要给螺纹设个到价提醒（只给建议） · 看看要不要设夜盘提醒，需要就设（需要时执行） · 在 3127 设上方提醒$/)).toBeInTheDocument();
});
