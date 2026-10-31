// @vitest-environment node
import { describe, expect, test } from "vitest";
import type { RuntimeStatus } from "@ai-trader/ui-events";
import { tqkqConnectionDetail, tqkqRecovery } from "./dataConnectionStatus";

const runtime: RuntimeStatus = {
  mode: "local",
  adapter: "native-agent",
  accountMode: "tqkq-readonly",
  runtimeConnected: true,
  agentConnected: true,
  marketDataStatus: "unavailable",
  accountStatus: "unavailable",
  tradingGatewayStatus: "unavailable",
  riskStatus: "unknown",
  connected: true,
  version: "0.8.6",
  dataConnectionStage: "failed",
  dataErrorCode: "tqkq_sidecar_importerror"
};

describe("TqKq connection detail", () => {
  test("names a failed data component", () => {
    expect(tqkqConnectionDetail(runtime)).toBe("交易数据组件未能启动，请检查应用更新");
  });

  test("shows a connected closed session separately from stale data", () => {
    expect(tqkqConnectionDetail({
      ...runtime,
      marketDataStatus: "normal",
      marketSessionStatus: "closed",
      accountStatus: "normal",
      dataConnectionStage: "ready",
      dataErrorCode: undefined,
    })).toBe("账户与行情已连接 · 当前休市，显示最新一笔行情");
  });

  test("explains a real reconnect without calling it a market break", () => {
    expect(tqkqConnectionDetail({
      ...runtime,
      marketDataStatus: "stale",
      accountStatus: "stale",
      dataConnectionStage: "reconnecting",
      dataErrorCode: undefined,
    })).toBe("网络连接中断，正在安全重连");
  });
});

test("快期 19:00–19:30 例行维护 says so instead of 网络中断 (owner 2026-10-21)", () => {
  const status: RuntimeStatus = { ...runtime, marketDataStatus: "stale", accountStatus: "stale",
    dataConnectionStage: "reconnecting", dataErrorCode: "tqkq_daily_maintenance" };
  expect(tqkqConnectionDetail(status)).toBe("快期每日 19:00–19:30 例行维护，结束后自动恢复");
  expect(tqkqRecovery(status, true)).toEqual({ action: "wait", label: "",
    detail: "快期每日 19:00–19:30 例行维护，期间行情和快期账户暂停更新，结束后自动恢复。" });
});

test("a session logged in after the close waits for the bell, not for 'data'", () => {
  const status: RuntimeStatus = { ...runtime, accountStatus: "normal", marketDataStatus: "stale", marketSessionStatus: "closed",
    dataConnectionStage: "waiting_market", dataErrorCode: "market_quote_not_ready" };
  expect(tqkqConnectionDetail(status)).toBe("已连接快期 · 当前休市，开盘后自动收到行情");
  expect(tqkqRecovery(status, true)).toEqual({ action: "none", label: "", detail: "快期已连接，当前休市；开盘后自动收到行情。" });
});

test("missing quote keeps the authenticated account connected and explains the data gap", () => {
  const status: RuntimeStatus = { ...runtime, accountStatus: "normal", marketDataStatus: "stale",
    dataConnectionStage: "waiting_market", dataErrorCode: "market_quote_not_ready" };
  expect(tqkqConnectionDetail(status)).toBe("部分合约报价暂缺，等待行情更新");
});
