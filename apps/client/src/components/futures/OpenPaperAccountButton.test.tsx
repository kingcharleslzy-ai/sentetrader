import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { openPaperAccount } from "../../api";
import { OpenPaperAccountButton } from "./OpenPaperAccountButton";

vi.mock("../../api", () => ({ openPaperAccount: vi.fn() }));

beforeEach(() => {
  vi.resetAllMocks();
});

test("开通本站模拟：一键开通并刷新账户目录", async () => {
  vi.mocked(openPaperAccount).mockResolvedValueOnce({ accountId: "paper:abc", displayName: "本站模拟", providerKind: "paper", isDefault: false });
  const onOpened = vi.fn().mockResolvedValue(undefined);
  render(<OpenPaperAccountButton baseUrl="https://runtime.test" onOpened={onOpened} />);
  fireEvent.click(screen.getByRole("button", { name: "开通本站模拟" }));
  await waitFor(() => expect(openPaperAccount).toHaveBeenCalledWith("https://runtime.test"));
  await waitFor(() => expect(onOpened).toHaveBeenCalled());
});

test("开通本站模拟失败时保留按钮并提示", async () => {
  vi.mocked(openPaperAccount).mockRejectedValueOnce(new Error("503"));
  render(<OpenPaperAccountButton baseUrl="https://runtime.test" onOpened={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "开通本站模拟" }));
  expect(await screen.findByText("开通未完成，请稍后重试")).toBeVisible();
  expect(screen.getByRole("button", { name: "开通本站模拟" })).toBeEnabled();
});
