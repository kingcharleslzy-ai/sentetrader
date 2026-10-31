import { runRemoteAction } from "./actions";

test("remote activation never submits an order, a confirmation or a form", () => {
  document.body.innerHTML = '<button id="trade">确认下单</button><form><button id="submit">发送</button></form><input id="safe" type="checkbox">';
  const trade = vi.fn(), submit = vi.fn();
  document.querySelector("#trade")!.addEventListener("click", trade);
  document.querySelector("form")!.addEventListener("submit", e => { e.preventDefault(); submit(); });
  (document.querySelector("#trade") as HTMLElement).focus(); runRemoteAction("activate");
  (document.querySelector("#submit") as HTMLElement).focus(); runRemoteAction("activate");
  expect(trade).not.toHaveBeenCalled(); expect(submit).not.toHaveBeenCalled();
  const safe = document.querySelector<HTMLInputElement>("#safe")!; safe.focus(); runRemoteAction("activate"); expect(safe.checked).toBe(true);
  document.body.innerHTML = "";
});

test("focus navigation enters from either end and wraps within the open dialog", () => {
  document.body.innerHTML = '<button id="outside">外部</button><dialog open><button id="first">第一项</button><button id="last">末项</button></dialog>';
  const rect = new DOMRect();
  const visible = vi.spyOn(HTMLElement.prototype, "getClientRects")
    .mockReturnValue(Object.assign([rect], { item: (index: number) => index === 0 ? rect : null }));
  try {
    const outside = document.querySelector<HTMLElement>("#outside")!;
    outside.focus(); runRemoteAction("focusPrevious"); expect(document.activeElement?.id).toBe("last");
    runRemoteAction("focusNext"); expect(document.activeElement?.id).toBe("first");
    outside.focus(); runRemoteAction("focusNext"); expect(document.activeElement?.id).toBe("first");
    runRemoteAction("focusPrevious"); expect(document.activeElement?.id).toBe("last");
  } finally { visible.mockRestore(); document.body.innerHTML = ""; }
});
