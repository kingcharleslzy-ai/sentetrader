import { cleanup, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

const freshExternalLinks = async () => {
  vi.resetModules();
  return await import("./externalLinks");
};

const clickOn = (element: Element) => {
  const event = new MouseEvent("click", { bubbles: true, cancelable: true, composed: true });
  element.dispatchEvent(event);
  return event;
};

afterEach(() => {
  cleanup();
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  invokeMock.mockReset();
});

it("installs no listener outside Tauri; openExternal keeps the ordinary new tab", async () => {
  const { installExternalLinkHandler, openExternal } = await freshExternalLinks();
  installExternalLinkHandler();
  const anchor = document.createElement("a");
  anchor.href = "https://account.shinnytech.com/";
  anchor.target = "_blank";
  document.body.append(anchor);
  expect(clickOn(anchor).defaultPrevented).toBe(false);
  expect(invokeMock).not.toHaveBeenCalled();
  const open = vi.fn();
  vi.stubGlobal("open", open);
  await openExternal("https://account.shinnytech.com/");
  expect(open).toHaveBeenCalledWith("https://account.shinnytech.com/", "_blank", "noopener,noreferrer");
});

it("opens a target=_blank https link through the Tauri command and cancels the WebView default", async () => {
  vi.stubGlobal("__TAURI_INTERNALS__", {});
  invokeMock.mockResolvedValue(undefined);
  const { installExternalLinkHandler } = await freshExternalLinks();
  installExternalLinkHandler();
  const anchor = document.createElement("a");
  anchor.href = "https://www.ndrc.gov.cn/x";
  anchor.target = "_blank";
  anchor.append(document.createElement("span"));
  document.body.append(anchor);
  // Clicking inner content proves the handler resolves the enclosing anchor.
  const event = clickOn(anchor.querySelector("span")!);
  expect(event.defaultPrevented).toBe(true);
  await waitFor(() => expect(invokeMock).toHaveBeenCalledWith("open_external_url", { url: "https://www.ndrc.gov.cn/x" }));
});

it("hands a web mail draft to the mail handler without opening a blank tab", async () => {
  const { openExternal } = await freshExternalLinks();
  const assign = vi.fn(), open = vi.fn();
  vi.stubGlobal("location", { assign });
  vi.stubGlobal("open", open);
  const url = "mailto:lzy619202200@163.com?subject=Sente&body=%E9%97%AE%E9%A2%98%0A%26%20%23";
  await openExternal(url);
  expect(assign).toHaveBeenCalledWith(url);
  expect(open).not.toHaveBeenCalled();
  expect(invokeMock).not.toHaveBeenCalled();
});

it("returns native mail-handler failure to the caller without logging the draft", async () => {
  vi.stubGlobal("__TAURI_INTERNALS__", {});
  const error = new Error("external_url_open_failed");
  invokeMock.mockRejectedValue(error);
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const { openExternal } = await freshExternalLinks();
  const url = "mailto:lzy619202200@163.com?subject=Sente&body=private%20feedback";
  await expect(openExternal(url)).rejects.toBe(error);
  expect(invokeMock).toHaveBeenCalledWith("open_external_url", { url });
  expect(warn).not.toHaveBeenCalled();
});

it("leaves same-origin and non-http links to the WebView", async () => {
  vi.spyOn(window, "open").mockReturnValue(null); // happy-dom 21 otherwise tries to navigate tauri://.
  vi.stubGlobal("__TAURI_INTERNALS__", {});
  const { installExternalLinkHandler } = await freshExternalLinks();
  installExternalLinkHandler();
  const sameOrigin = document.createElement("a");
  sameOrigin.href = new URL("/positions", window.location.href).href;
  document.body.append(sameOrigin);
  expect(clickOn(sameOrigin).defaultPrevented).toBe(false);
  const internal = document.createElement("a");
  internal.href = "tauri://localhost/positions";
  internal.target = "_blank";
  document.body.append(internal);
  expect(clickOn(internal).defaultPrevented).toBe(false);
  const mail = document.createElement("a");
  mail.href = "mailto:lzy619202200@163.com?body=private%20feedback";
  let intercepted = false;
  mail.addEventListener("click", event => { intercepted = event.defaultPrevented; event.preventDefault(); });
  document.body.append(mail);
  clickOn(mail);
  expect(intercepted).toBe(false);
  expect(invokeMock).not.toHaveBeenCalled();
});

it("intercepts a cross-origin link without target and only warns when the command fails", async () => {
  vi.stubGlobal("__TAURI_INTERNALS__", {});
  invokeMock.mockRejectedValue(new Error("external_url_forbidden"));
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const { installExternalLinkHandler } = await freshExternalLinks();
  installExternalLinkHandler();
  const anchor = document.createElement("a");
  anchor.href = "https://example.com/article";
  document.body.append(anchor);
  expect(clickOn(anchor).defaultPrevented).toBe(true);
  await waitFor(() => expect(warn).toHaveBeenCalledWith("external link not opened", "https://example.com/article", expect.anything()));
  expect(invokeMock).toHaveBeenCalledWith("open_external_url", { url: "https://example.com/article" });
});
