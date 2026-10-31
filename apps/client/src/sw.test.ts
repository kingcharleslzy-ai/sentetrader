// @vitest-environment node
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { expect, test, vi } from "vitest";

test.each(["https://qilinshuzhi.com/trader/", "https://qilinshuzhi.com/trader/admin"])(
  "管理员通知从 %s 打开服务器页", async (current) => {
    const listeners: Record<string, (event: unknown) => void> = {};
    const client = { url: current, navigate: vi.fn(), focus: vi.fn(), postMessage: vi.fn() };
    const clients = { matchAll: vi.fn().mockResolvedValue([client]), openWindow: vi.fn() };
    runInNewContext(readFileSync(new URL("./sw.js", import.meta.url), "utf8"), {
      self: { registration: { scope: "https://qilinshuzhi.com/trader/" }, clients,
        addEventListener: (name: string, callback: (event: unknown) => void) => { listeners[name] = callback; } }, URL,
    });
    const target = "https://qilinshuzhi.com/trader/admin?section=servers";
    let finished: Promise<unknown> = Promise.resolve();
    listeners.notificationclick({ notification: { close: vi.fn(), data: { url: target } },
      waitUntil: (promise: Promise<unknown>) => { finished = promise; } });
    await finished;
    expect(client.navigate).toHaveBeenCalledWith(target);
    expect(client.focus).toHaveBeenCalledOnce();
    expect(client.postMessage).not.toHaveBeenCalled();
    expect(clients.openWindow).not.toHaveBeenCalled();
  },
);
