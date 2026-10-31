import { expect, test } from "vitest";
import { parseDeepLink } from "./deepLink";

test("外盘复盘的推送链接带美元子账户：点开先切到那个账户再看那天的复盘（2026-10-21）", () => {
  expect(parseDeepLink("review:2026-10-21")).toEqual({ kind: "review", day: "2026-10-21" });
  expect(parseDeepLink("review:2026-10-21:paperusd:f1990d30ada5"))
    .toEqual({ kind: "review", day: "2026-10-21", accountId: "paperusd:f1990d30ada5" });
  expect(parseDeepLink("review:2026-10-21:tqkq-sim:x")).toBeNull();
  expect(parseDeepLink("review:yesterday")).toBeNull();
});

test("开盘前的推送直达今日作战表（第 5 批，2026-10-25）；旧的 research 链接仍到品种调研", () => {
  expect(parseDeepLink("research:sheet")).toEqual({ kind: "research", page: "sheet" });
  expect(parseDeepLink("research")).toEqual({ kind: "research" });
});
