import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FeedbackSettings } from "./FeedbackSettings";
import { composeFeedback, feedbackDiagnostics, feedbackMailto, type FeedbackEnvironment } from "./feedback";

const openExternal = vi.hoisted(() => vi.fn());
vi.mock("../../externalLinks", () => ({ openExternal }));

const env: FeedbackEnvironment = { version: "1.7.2", build: "cdb7649", platform: "web", runtimeVersion: null, mode: "cloud", connection: "disconnected" };
const browser = { language: "zh-CN", theme: "dark", timezone: "Asia/Shanghai", viewport: "1440×960" };
const at = new Date("2026-10-06T03:40:43Z");

function stubClipboard(writeText: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn(writeText) } });
  return navigator.clipboard.writeText as ReturnType<typeof vi.fn>;
}
const descriptionField = () => screen.getByLabelText("遇到了什么问题");

beforeEach(() => { openExternal.mockReset(); });

// Protects: the diagnostics carry only the whitelisted environment fields, and unknown values stay unknown.
test("diagnostics are a fixed whitelist and unknown Runtime/connection read as unknown", () => {
  const diagnostics = JSON.parse(feedbackDiagnostics(env, browser, at));
  expect(Object.keys(diagnostics)).toEqual(["app", "clientVersion", "build", "platform", "runtimeVersion", "mode", "connection",
    "language", "theme", "timezone", "viewport", "collectedAt"]);
  expect(diagnostics).toMatchObject({ runtimeVersion: null, connection: "disconnected" });
  const { body } = composeFeedback({ kind: "problem", description: "x", expectation: "", env, occurredAt: at, diagnostics: null });
  expect(body).toContain("Runtime：未知");
  expect(body).toContain("云端：未连接");
  expect(body).not.toContain("基础诊断");
});

// Protects: user text never reaches the mail headers, and the body survives one round of encoding intact.
test("the subject is fixed and the mailto body keeps Chinese, line breaks and & ? # after one encoding", () => {
  const description = "回放卡住\nBcc: other@example.com & a=1?b#c";
  const draft = composeFeedback({ kind: "idea", description, expectation: "", env, occurredAt: at, diagnostics: null });
  expect(draft.subject).toBe("Sente 反馈 · 提建议 · 1.7.2");
  const url = new URL(feedbackMailto(draft));
  expect(`${url.protocol}${url.pathname}`).toBe("mailto:lzy619202200@163.com");
  expect([...url.searchParams.keys()]).toEqual(["subject", "body"]);
  expect(url.searchParams.get("subject")).toBe(draft.subject);
  expect(url.searchParams.get("body")).toBe(draft.body.replace(/\n/g, "\r\n"));
  expect(feedbackMailto(draft)).toContain("%0D%0ABcc%3A%20other%40example.com%20%26%20a%3D1%3Fb%23c");
  expect(feedbackMailto(draft)).not.toMatch(/[\n#]|&(?!body=)/);
});

// Protects: nothing leaves without a description, and diagnostics default on but can be withdrawn.
test("an empty description is stopped; diagnostics are attached by default and dropped when switched off", async () => {
  const setItem = vi.spyOn(Storage.prototype, "setItem");
  const writeText = stubClipboard(async () => undefined);
  render(<FeedbackSettings environment={env} />);
  fireEvent.click(screen.getByRole("button", { name: "打开邮件草稿" }));
  fireEvent.click(screen.getByRole("button", { name: "复制反馈内容" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("请写下遇到了什么问题");
  expect(openExternal).not.toHaveBeenCalled();
  expect(writeText).not.toHaveBeenCalled();

  fireEvent.change(descriptionField(), { target: { value: "图表不动" } });
  expect(descriptionField()).toHaveAttribute("data-agent-private");
  expect(screen.getByRole("switch", { name: "附上基础诊断" })).toHaveAttribute("aria-checked", "true");
  fireEvent.click(screen.getByRole("button", { name: "复制反馈内容" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
  expect(writeText.mock.calls[0][0]).toContain("基础诊断：");
  expect(writeText.mock.calls[0][0]).not.toMatch(/userAgent|token|account|https?:/);
  expect(await screen.findByRole("status")).toHaveTextContent("已复制到剪贴板");
  expect(screen.queryByLabelText("反馈内容（手动复制）")).toBeNull();

  fireEvent.click(screen.getByRole("switch", { name: "附上基础诊断" }));
  fireEvent.click(screen.getByRole("button", { name: "复制反馈内容" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2));
  expect(writeText.mock.calls[1][0]).not.toContain("基础诊断");
  expect(setItem).not.toHaveBeenCalled();
  setItem.mockRestore();
});

// Protects: a refused clipboard never pretends to have copied; the full text stays selectable and agent-private.
test("a denied clipboard shows the full draft read-only for manual copy", async () => {
  stubClipboard(async () => { throw new DOMException("denied", "NotAllowedError"); });
  render(<FeedbackSettings environment={env} />);
  fireEvent.change(descriptionField(), { target: { value: "下单后没回执" } });
  fireEvent.click(screen.getByRole("button", { name: "复制反馈内容" }));
  const manual = await screen.findByLabelText("反馈内容（手动复制）");
  expect(manual).toHaveAttribute("readonly");
  expect(manual).toHaveAttribute("data-agent-private");
  expect((manual as HTMLTextAreaElement).value).toMatch(/^收件人：lzy619202200@163\.com\n主题：Sente 反馈 · 遇到问题 · 1\.7\.2\n\n[\s\S]*下单后没回执/);
  expect(screen.getByRole("status")).not.toHaveTextContent("已复制");
});

// Protects: opening only requests the user's mail program — never claimed as sent — and a failure keeps the input.
test("opening the draft reports a request only, and a failed open keeps the input with a manual fallback", async () => {
  render(<FeedbackSettings environment={env} />);
  fireEvent.change(descriptionField(), { target: { value: "语音没声音" } });
  fireEvent.change(screen.getByLabelText("期待的结果"), { target: { value: "能听到回复" } });
  openExternal.mockResolvedValueOnce(undefined);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "打开邮件草稿" })); });
  expect(openExternal).toHaveBeenCalledWith(expect.stringMatching(/^mailto:lzy619202200@163\.com\?subject=/));
  expect(screen.getByRole("status")).toHaveTextContent("已请求打开，请在邮件中确认并发送");
  expect(document.body).not.toHaveTextContent(/已发送|已提交|发送成功/);
  expect(screen.getByText("这只是草稿，尚未发送")).toBeInTheDocument();
  expect(screen.getByLabelText("正文")).toHaveAttribute("data-agent-private");

  openExternal.mockRejectedValueOnce(new Error("external_url_open_failed"));
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "打开邮件草稿" })); });
  expect(screen.getByRole("status")).toHaveTextContent("没能打开邮件程序");
  expect(descriptionField()).toHaveValue("语音没声音");
  expect(screen.getByLabelText("期待的结果")).toHaveValue("能听到回复");
  expect((screen.getByLabelText("正文") as HTMLTextAreaElement).value).toContain("语音没声音");
  expect(screen.getByRole("button", { name: "复制反馈内容" })).toBeEnabled();
});

// Protects: the two text fields are named by their labels (screen readers and the observation snapshot), not the example placeholder.
test("the description and expectation fields carry their labels as names", () => {
  render(<FeedbackSettings environment={env} />);
  const named = (text: string) => {
    const field = screen.getByLabelText(text);
    expect(document.getElementById(field.getAttribute("aria-labelledby") ?? "")?.textContent).toBe(text);
  };
  named("遇到了什么问题");
  named("期待的结果");
});
