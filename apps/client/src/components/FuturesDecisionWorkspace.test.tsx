// @vitest-environment jsdom
// Stays on jsdom: its assertions fail under the default happy-dom (accessible names / text / location differ).
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, vi } from "vitest";
import type {
  AccountOverviewSnapshot,
  InstrumentCatalog,
  MarketWorkbenchSnapshot
} from "../api";
import { rememberStrategyScopes } from "../api";
import { GuestAppearanceContext } from "../guestAppearance";
import { FuturesDecisionWorkspace } from "./FuturesDecisionWorkspace";
import { DEFAULT_PREFERENCES } from "../userPreferences";
import { resetShortcutStore } from "../shortcuts";
import { readQueryClient, resetReadIdentity } from "../runtimeClient";
import * as uiControl from "../hooks/useUiControl";
import * as headerFit from "./futures/useHeaderFit";
import { applyUiSurface, uiSurface } from "../uiSurfaces";
import { resetAiAnnotations } from "../aiAnnotations";
import { discardDraft, newDraft, resetStrategyEditor } from "../strategyEditorStore";
import { CLIENT_BUILD, noteRuntimeCompat, noteWebBuild, resetClientCompat, watchWebBuild, webPage } from "../clientBuild";
import { pushToTalkStore } from "../pushToTalk";
import { voiceCallStore } from "../voiceRealtime";
import { applyFeedEvent } from "../runtimeFeed";
import { voiceReply } from "../voiceReply";
import { installPttFakes, removePttFakes } from "../../tests/support/pttFakes";
import { MOBILE_LAYOUT_QUERY } from "../hooks/useMobileLayout";

const nativeWindow = vi.hoisted(() => ({ startDragging: vi.fn(async () => undefined) }));
vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => nativeWindow }));

vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2 },
  CandlestickSeries: {},
  HistogramSeries: {},
  LineSeries: {},
  createSeriesMarkers: () => ({ detach: vi.fn() }),
  createChart: () => {
    const series = {
      setData: vi.fn(),
      applyOptions: vi.fn(),
      priceScale: () => ({ applyOptions: vi.fn() }),
      createPriceLine: vi.fn(() => ({})),
      removePriceLine: vi.fn(),
      attachPrimitive: vi.fn(),
      detachPrimitive: vi.fn(),
      coordinateToPrice: vi.fn(() => 821.5)
    };
    return {
      addSeries: () => series,
      paneSize: () => ({height:300,width:600}),
      applyOptions: vi.fn(),
      timeScale: () => ({
        fitContent: vi.fn(),
        getVisibleRange: vi.fn(() => null),
        getVisibleLogicalRange: vi.fn(() => ({ from: 0, to: 10 })),
        setVisibleLogicalRange: vi.fn(),
        subscribeVisibleLogicalRangeChange: vi.fn(),
        unsubscribeVisibleLogicalRangeChange: vi.fn()
      }),
      subscribeClick: vi.fn(),
      unsubscribeClick: vi.fn(),
      subscribeCrosshairMove: vi.fn(),
      unsubscribeCrosshairMove: vi.fn(),
      remove: vi.fn()
    };
  }
}));

const market: MarketWorkbenchSnapshot = {
  schemaVersion: 1,
  durationSeconds: 300,
  capturedAt: "2026-09-01T06:59:52Z",
  ageSeconds: 0.3,
  dataStatus: "normal",
  marketSessionStatus: "trading",
  isCached: false,
  liveTrading: "disabled",
  quote: {
    symbol: "SHFE.au2612",
    name: "沪金2612",
    quoteDatetime: "2026-09-01T14:59:52+08:00",
    lastPrice: 824,
    changePercent: -0.68,
    bidPrice1: 823.98,
    bidVolume1: 42,
    askPrice1: 824.02,
    askVolume1: 36,
    open: 829.8,
    highest: 831.6,
    lowest: 822.4,
    volume: 128_564,
    openInterest: 182_746,
    priceTick: 0.02,
    volumeMultiple: 1000,
    upperLimit: 900,
    lowerLimit: 700
  },
  bars: [{
    datetime: 1_788_192_000_000_000_000,
    open: 820,
    high: 825,
    low: 818,
    close: 824,
    volume: 30,
    openInterest: 182_746
  }]
};

const instruments: InstrumentCatalog = {
  schemaVersion: 1,
  capturedAt: market.capturedAt,
  dataStatus: "normal",
  isCached: false,
  count: 1,
  items: [{
    symbol: "SHFE.au2612",
    exchangeId: "SHFE",
    instrumentId: "au2612",
    instrumentName: "沪金2612",
    productId: "au",
    priceTick: 0.02,
    volumeMultiple: 1000,
    deliveryYear: 2026,
    deliveryMonth: 12,
    expireRestDays: 90,
    hasNight: true,
    isMain: true
  }]
};

const account: AccountOverviewSnapshot = {
  account: {
    accountName: "快期模拟账户",
    capturedAt: market.capturedAt,
    ageSeconds: 0.3,
    dataStatus: "normal",
    equity: 1_026_580,
    todayPnl: 2_340,
    marginRatio: 0.12,
    available: 903_390,
    margin: 123_190,
    riskRatio: 12,
    positionCount: 1
  },
  positions: [{
    symbol: "SHFE.au2612",
    direction: "long",
    volume: 2,
    todayVolume: 2,
    historicalVolume: 0,
    frozenToday: 0,
    frozenHistorical: 0,
    unrealizedPnl: 2_340
  }],
  activity: {
    schemaVersion: 1,
    capturedAt: market.capturedAt,
    ageSeconds: 0.3,
    dataStatus: "normal",
    ordersInitialized: true,
    orders: [],
    trades: [],
    liveTrading: "disabled"
  },
  risk: {
    configured: true,
    dataStatus: "normal",
    status: "normal",
    message: "正常"
  }
};

beforeEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  resetShortcutStore();
  resetAiAnnotations();
});

test("agent context marks a retained chart stale after refresh fails", () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const values = props({marketError: "当前行情暂时不可用"});
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  try {
    expect(control.mock.calls.at(-1)?.[4].market).toMatchObject({
      dataStatus: "stale", isCached: true, capturedAt: market.capturedAt, readError: "当前行情暂时不可用",
    });
    view.rerender(<FuturesDecisionWorkspace {...values as any} marketError={undefined} />);
    expect(control.mock.calls.at(-1)?.[4].market).toMatchObject({dataStatus: "normal", isCached: false, readError: null});
  } finally {
    view.unmount(); control.mockRestore();
  }
});

test("窗口心跳的 connection 段上报客户端构建与 Runtime 兼容判定", () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  // The refresh verdict is the page's own comparison against the deployed version.json,
  // not a Runtime answer (tests build with commit "dev", so spell a real one here).
  const previousCommit = CLIENT_BUILD.commit;
  CLIENT_BUILD.commit = "f8f50e5c0b9d7a2e4d6c8a1b3e5f7d9c0a2b4e6f";
  noteRuntimeCompat(new Headers({ "x-ai-trader-client-compat": "ok", "x-ai-trader-runtime": "1.6.6+deadbee" }));
  noteWebBuild("1685e2b0c0ffee0000000000deadbeef1234");
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  try {
    expect(control.mock.calls.at(-1)?.[4].connection).toMatchObject({
      build: "f8f50e5c0b9d7a2e4d6c8a1b3e5f7d9c0a2b4e6f", compat: "refresh",
    });
  } finally {
    view.unmount(); control.mockRestore(); CLIENT_BUILD.commit = previousCommit; resetClientCompat();
  }
});

test("设置 → 关于的软件版本行显示构建号，Runtime 判定过旧时旁边提示", () => {
  const updater = { phase: "current" as const, info: { currentVersion: "1.6.5", available: false, version: "1.6.5" }, check: vi.fn(), install: vi.fn() };
  noteRuntimeCompat(new Headers({ "x-ai-trader-client-compat": "outdated", "x-ai-trader-min-client": "1.6.6" }));
  const { rerender } = render(<FuturesDecisionWorkspace {...props({ updater }) as any} />);
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  fireEvent.click(screen.getByRole("button", { name: "关于" }));
  const settings = screen.getByRole("dialog", { name: "设置" });
  expect(within(settings).getByText("软件版本").closest(".app-setting-row")).toHaveTextContent("1.6.5（dev）");
  expect(within(settings).getByText("Runtime 要求客户端至少 1.6.6，请更新客户端")).toBeVisible();

  resetClientCompat();
  rerender(<FuturesDecisionWorkspace {...props({ updater }) as any} />);
  expect(within(settings).queryByText(/请更新客户端/)).not.toBeInTheDocument();
  expect(within(settings).queryByText(/刷新页面即可更新/)).not.toBeInTheDocument();
});

test("软件版本行在网页构建落后于已部署 version.json 时提示刷新", () => {
  const updater = { phase: "current" as const, info: { currentVersion: "1.6.5", available: false, version: "1.6.5" }, check: vi.fn(), install: vi.fn() };
  const previousCommit = CLIENT_BUILD.commit;
  CLIENT_BUILD.commit = "f8f50e5c0b9d7a2e4d6c8a1b3e5f7d9c0a2b4e6f";
  noteWebBuild("1685e2b0c0ffee0000000000deadbeef1234");
  const view = render(<FuturesDecisionWorkspace {...props({ updater }) as any} />);
  try {
    fireEvent.click(screen.getByRole("button", { name: "设置" }));
    fireEvent.click(screen.getByRole("button", { name: "关于" }));
    const settings = screen.getByRole("dialog", { name: "设置" });
    expect(within(settings).getByText("网页有新版本，点检查更新或刷新页面即可更新")).toBeVisible();
  } finally {
    view.unmount(); CLIENT_BUILD.commit = previousCommit; resetClientCompat();
  }
});

function props(overrides: Record<string, unknown> = {}) {
  return {
    state: { turns: [], runtime: { simulationTrading: true } },
    mode: "local" as const,
    connection: "connected" as const,
    status: { runtimeUsable: true },
    instruments,
    instrumentsLoading: false,
    account,
    market,
    marketLoading: false,
    duration: 300 as const,
    selectedSymbol: "SHFE.au2612",
    simulationOrders: [],
    aiExecution: {
      authorization: null,
      stops: [],
      simulationOnly: true,
      liveTrading: "disabled"
    },
    aiExecutionBusy: false,
    agentAccount: {
      available: true,
      authenticated: true,
      loginRequired: false,
      authMode: null,
      planType: null
    },
    simulationGatewayAvailable: true,
    simulationEnabled: true,
    tradeBusy: false,
    fontScale: "large" as const,
    interfaceDensity: "standard" as const,
    onFontScaleChange: vi.fn(),
    onInterfaceDensityChange: vi.fn(),
    simulationLimits: { enabled: false, maxOrderVolume: 10, maxOrders: 100 },
    onSimulationLimitsChange: vi.fn(),
    proposalActions: {
      adjustRisk: vi.fn(), confirm: vi.fn(), reject: vi.fn(), cancelRun: vi.fn(),
      changeStrategyMonitoring: vi.fn(),
      loadStrategyMonitor: vi.fn()
    },
    updater: { phase: "current" as const, check: vi.fn(), install: vi.fn() },
    onSelectSymbol: vi.fn(),
    onDurationChange: vi.fn(),
    onRefreshMarket: vi.fn(),
    onRefreshAccount: vi.fn(),
    onRetryInstruments: vi.fn(),
    onManualSubmit: vi.fn(async () => undefined),
    onTargetPosition: vi.fn(async () => true),
    onOrderAction: vi.fn(),
    onStopAiExecution: vi.fn(),
    onConfigureAgent: vi.fn(),
    onPrompt: vi.fn(async () => undefined),
    onOpenCloudLogin: vi.fn(),
    onToggleMode: vi.fn(),
    onOpenReplay: vi.fn(),
    ...overrides
  };
}

function openAiPanel() {
  fireEvent.click(screen.getByRole("button", { name: "打开对话" }));
}

/** The quick-order confirmation: jsdom has no showModal, so the dialog is queried by its test id. */
function confirmQuickOrder() {
  fireEvent.click(within(screen.getByTestId("quick-order-dialog")).getByRole("button", { name: "确认提交" }));
}

function openOrderPanel() {
  const closed = screen.queryByRole("button", { name: "打开模拟下单" });
  if (closed) fireEvent.click(closed);
}

function openPositionsPanel() {
  // The bar is mounted collapsed by default (its line is the account overview); expand it for the tabs.
  const collapsed = screen.queryByRole("button", { name: "展开持仓与委托" });
  if (collapsed) fireEvent.click(collapsed);
}

test("云端账户连接不根据本机快期凭据显示未配置", () => {
  render(<FuturesDecisionWorkspace {...props({ mode: "cloud", state: { turns: [], runtime: { accountMode: "tqkq-readonly", accountStatus: "normal", simulationTrading: true } } }) as any} />);
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  expect(screen.getByText("账户连接").closest(".app-setting-row")).toHaveTextContent("已连接");
  expect(screen.queryByText("未配置")).not.toBeInTheDocument();
});

test("模型未就绪时只显示一次明确入口并禁用 AI 交易和输入", () => {
  const values = props({
    agentAccount: {
      available: true,
      authenticated: false,
      loginRequired: false,
      authMode: null,
      planType: null
    }
  });
  render(<FuturesDecisionWorkspace {...values as any} />);
  openAiPanel();

  expect(screen.getByTestId("agent-login")).toHaveTextContent("模型尚未就绪");
  expect(screen.queryByRole("button", { name: /登录/ })).not.toBeInTheDocument();
  expect(screen.queryByTestId("ai-execution-control")).not.toBeInTheDocument();
  expect(screen.getByLabelText("向 AI 决策流提问")).toBeDisabled();
});

test("DeepSeek 未配置时说明由服务器配置，不再引导本机填 key", () => {
  const values = props({
    agentAccount: {
      available: false,
      authenticated: false,
      loginRequired: false,
      configurationRequired: true,
      credentialConfigured: false,
      provider: "deepseek",
      providerName: "DeepSeek",
      model: "deepseek-v4-flash"
    }
  });
  render(<FuturesDecisionWorkspace {...values as any} />);
  openAiPanel();

  // Model keys live with the Runtime: the client can only point at the model picker.
  expect(screen.getByTestId("agent-login")).toHaveTextContent("服务器尚未配置 DeepSeek");
  expect(screen.queryByRole("button", { name: /配置 DeepSeek/ })).not.toBeInTheDocument();
  expect(screen.getByLabelText("向 AI 决策流提问")).toHaveAttribute("placeholder", "模型尚未就绪，请在设置里选择模型");
});

test("DeepSeek 密钥已保存但模型未连接时不谎称连接成功", () => {
  const values = props({
    agentAccount: {
      available: false,
      authenticated: false,
      loginRequired: false,
      configurationRequired: false,
      credentialConfigured: true,
      provider: "deepseek",
      providerName: "DeepSeek",
      model: "deepseek-v4-flash",
      error: "agent_auth_failed"
    }
  });
  render(<FuturesDecisionWorkspace {...values as any} />);
  openAiPanel();

  expect(screen.getByTestId("agent-login")).toHaveTextContent("模型尚未连接");
  expect(screen.queryByRole("button", { name: "检查配置" })).not.toBeInTheDocument();
});

test("AI requests reach the agent; global stop is in settings and requires confirmation", async () => {
  const values = props();
  const { rerender } = render(<FuturesDecisionWorkspace {...values as any} />);
  openAiPanel();

  expect(screen.queryByTestId("ai-execution-control")).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "向 AI 决策流提问" }), {
    target: { value: "请开启 AI 自动交易" }
  });
  fireEvent.click(screen.getByRole("button", { name: "发送给AI" }));
  await waitFor(() => expect(values.onPrompt).toHaveBeenCalledWith(
    "请开启 AI 自动交易", expect.any(Object)
  ));
  expect(screen.queryByTestId("ai-execution-request")).not.toBeInTheDocument();

  const activeValues = props({
    aiExecution: {
      authorization: {
        authorizationId: "auth-1",
        userId: "local-user",
        sessionId: "session-1",
        status: "ACTIVE",
        allowedSymbols: ["SHFE.au2612"],
        permissions: ["open", "cancel", "close", "stop"],
        maxOrderVolume: 1,
        maxOrders: 10,
        usedOrders: 2,
        remainingOrders: 8,
        expiresAt: "2026-09-02T08:30:00+08:00",
        createdAt: "2026-09-02T08:00:00+08:00",
        updatedAt: "2026-09-02T08:00:00+08:00",
        environment: "TqKq simulation",
        liveTrading: "disabled"
      },
      stops: [],
      simulationOnly: true,
      liveTrading: "disabled"
    }
  });
  rerender(<FuturesDecisionWorkspace {...activeValues as any} />);

  expect(screen.queryByRole("button", {name:/急停/})).toBeNull();
  fireEvent.click(screen.getByRole("button", {name:"设置"}));
  fireEvent.click(screen.getByRole("button", {name:"交易"}));
  fireEvent.click(screen.getByRole("button", { name: "打开高级交易设置" }));
  fireEvent.click(screen.getByRole("button", {name:"全局急停"}));
  expect(activeValues.onStopAiExecution).not.toHaveBeenCalled();
  expect(screen.getByText(/不会自动平仓/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name:"确认全局急停"}));
  expect(activeValues.onStopAiExecution).toHaveBeenCalledWith(true);
});

test("模拟盘默认无平台上限并可在设置中启用自定义限制", () => {
  const values = props();
  const { rerender } = render(<FuturesDecisionWorkspace {...values as any} />);

  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  fireEvent.click(screen.getByRole("button", { name: "交易" }));
  fireEvent.click(screen.getByRole("button", { name: "打开高级交易设置" }));
  expect(screen.getByRole("region", { name: "执行限制" })).toHaveTextContent("未启用");
  fireEvent.click(screen.getByRole("switch", { name: "平台上限" }));
  expect(values.onSimulationLimitsChange).toHaveBeenCalledWith({
    enabled: true, maxOrderVolume: 10, maxOrders: 100
  });

  rerender(<FuturesDecisionWorkspace {...props({
    simulationLimits: { enabled: true, maxOrderVolume: 12, maxOrders: 80 }
  }) as any} />);
  expect(screen.getByRole("spinbutton", { name: "单笔最多手数" })).toHaveValue(12);
  expect(screen.getByRole("spinbutton", { name: "累计最多委托" })).toHaveValue(80);
});

test("建议问题：对话面板打开才按当前页面向 AI 要，最多 2 条，点一下填入不发送；换页面后接口 404 时什么都不显示", async () => {
  const asked: Record<string, unknown>[] = [];
  let missing = false;
  plansRuntime({ owner: true, accounts: { current: "paper:u1", accounts: [] }, suggest: body => {
    asked.push(body);
    return missing ? new Response("not found", { status: 404 })
      : new Response(JSON.stringify({ items: ["黄金夜盘怎么看", "要不要设个止损", "第三条不显示"] }), { headers: { "Content-Type": "application/json" } });
  } });
  const values = props({ mode: "cloud", runtimeBaseUrl: "https://runtime.test", chatSessionId: "s-1" });
  render(<FuturesDecisionWorkspace {...values as any} />);
  await act(async () => { await Promise.resolve(); });
  expect(asked).toEqual([]);  // the panel is closed: nothing is asked
  openAiPanel();
  const group = await screen.findByLabelText("建议问题");
  expect(within(group).getAllByRole("button").map(button => button.textContent)).toEqual(["黄金夜盘怎么看", "要不要设个止损"]);
  expect(asked).toEqual([{ sessionId: "s-1", symbol: "SHFE.au2612", view: "trade", trigger: "open" }]);
  expect(screen.getByText("在K 线图可以这样问")).toBeVisible();
  fireEvent.click(within(group).getByRole("button", { name: "要不要设个止损" }));
  const input = screen.getByRole("textbox", { name: "向 AI 决策流提问" });
  expect(input).toHaveValue("要不要设个止损");
  expect(input).toHaveFocus();
  expect(values.onPrompt).not.toHaveBeenCalled();

  // Another view: the old ones go at once; the Runtime without the route (404) means none at all.
  missing = true;
  fireEvent.click(screen.getByRole("button", { name: "报价" }));
  expect(screen.queryByLabelText("建议问题")).toBeNull();
  await waitFor(() => expect(asked.at(-1)).toMatchObject({ view: "board", trigger: "symbol" }), { timeout: 3000 });
  await act(async () => { await Promise.resolve(); });
  expect(screen.queryByLabelText("建议问题")).toBeNull();
  expect(screen.queryByText(/可以这样问/)).toBeNull();
  expect(values.onPrompt).not.toHaveBeenCalled();
});

test("顶栏不显示交易账户切换；设置 → 账户里按“账户名 / 本站模拟”列出，非本人账户才带角色", async () => {
  plansRuntime({ owner: false, accounts: { current: "tqkq:u1", accounts: [
    { id: "tqkq:u1", displayName: "lzy393705181", role: "owner", environment: "simulation", executionOwner: true, providerKind: "tqkq-sidecar" },
    { id: "paper:u1", displayName: "本站模拟账户", role: "owner", environment: "simulation", executionOwner: true, providerKind: "paper" },
    { id: "tqkq:shared", displayName: "团队账户", role: "viewer", environment: "simulation", executionOwner: false, providerKind: "tqkq-sidecar" },
  ] } });
  render(<FuturesDecisionWorkspace {...props({ mode: "cloud", runtimeBaseUrl: "https://basic.example.test", chatSessionId: "s-1" }) as any} />);
  fireEvent.click(screen.getByTitle("设置"));
  const settings = screen.getByRole("dialog", { name: "设置" });
  const select = await within(settings).findByRole("combobox", { name: "切换交易账户" });
  // 2026-10-08: grouped 模拟盘 / 实盘 — every account is a simulation one; 实盘 has only its 暂无 entry.
  expect([...(select as HTMLSelectElement).querySelectorAll("optgroup")].map(group => [group.label, [...group.querySelectorAll("option")].map(option => option.text)]))
    .toEqual([["模拟盘", ["lzy393705181", "本站模拟", "团队账户（只读）"]], ["实盘", ["暂无（未接入期货公司）"]]]);
  expect(screen.getAllByTestId("trading-account-select")).toHaveLength(1);
  expect(document.querySelector(".fdw-header [data-testid='trading-account-select']")).toBeNull();
  expect(document.querySelector(".fdw-header")).not.toHaveTextContent("lzy393705181");
});

test("自动会话结束后仍在设置中显示未决保护单并确认急停", () => {
  const values = props({
    aiExecution: {
      authorization: null,
      stops: [{
        stopId: "stop-1", authorizationId: "auth-1", sessionId: "session-1",
        symbol: "SHFE.au2612", positionDirection: "long", positionBucket: "today",
        volume: 1, triggerPrice: 820, priceIntent: "aggressive", status: "ARMED",
        createdAt: "2026-09-02T08:00:00+08:00", updatedAt: "2026-09-02T08:00:00+08:00",
        liveTrading: "disabled"
      }],
      simulationOnly: true,
      liveTrading: "disabled"
    }
  });
  render(<FuturesDecisionWorkspace {...values as any} />);
  openAiPanel();

  fireEvent.click(screen.getByRole("button", {name:"设置"}));
  fireEvent.click(screen.getByRole("button", {name:"交易"}));
  fireEvent.click(screen.getByRole("button", { name: "打开高级交易设置" }));
  expect(screen.getByText("1 个待触发保护单")).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name:"全局急停"}));
  expect(values.onStopAiExecution).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name:"确认全局急停"}));
  expect(values.onStopAiExecution).toHaveBeenCalledWith(true);
});

test("只停新开仓是直接动作，全局急停仍需确认", () => {
  const values = props();
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(screen.getByRole("button", {name:"设置"}));
  fireEvent.click(screen.getByRole("button", {name:"交易"}));
  fireEvent.click(screen.getByRole("button", { name: "打开高级交易设置" }));
  const limits = screen.getByRole("region", { name: "执行限制" });
  fireEvent.click(within(limits).getByRole("button", {name:"只停新开仓"}));
  expect(values.onStopAiExecution).toHaveBeenCalledWith(false, "opens");
  expect(screen.queryByRole("dialog", { name: "全局急停 AI 模拟执行" })).not.toBeInTheDocument();
  fireEvent.click(within(limits).getByRole("button", {name:"全局急停"}));
  expect(values.onStopAiExecution).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", {name:"确认全局急停"}));
  expect(values.onStopAiExecution).toHaveBeenCalledWith(true);
});

test("只停新开仓状态下提供恢复入口，且仍可升级为全局急停", async () => {
  const resume = vi.fn().mockResolvedValue(true);
  const values = props({aiExecution: {controls: {stopStatus: "HALT_OPENS"}, authorization: null, stops: []}, onResumeAiExecution: resume});
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(screen.getByRole("button", {name:"已只停新开仓 · 恢复"}));
  expect(screen.getByRole("dialog", { name: "恢复 AI 模拟执行" })).toBeVisible();
  expect(resume).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "暂不恢复" }));
  fireEvent.click(screen.getByRole("button", {name:"设置"}));
  fireEvent.click(screen.getByRole("button", {name:"交易"}));
  fireEvent.click(screen.getByRole("button", { name: "打开高级交易设置" }));
  const limits = screen.getByRole("region", { name: "执行限制" });
  expect(within(limits).getByText("已只停新开仓")).toBeVisible();
  fireEvent.click(within(limits).getByRole("button", {name:"全局急停"}));
  fireEvent.click(screen.getByRole("button", {name:"确认全局急停"}));
  expect(values.onStopAiExecution).toHaveBeenCalledWith(true);
});

test("布局入口可交换辅助区并用键盘在边界内调整抽屉宽度", () => {
  const values = props();
  render(<FuturesDecisionWorkspace {...values as any} />);
  const workspace = screen.getByTestId("futures-decision-workspace");

  fireEvent.click(screen.getByRole("button", { name: "3分" }));
  expect(values.onDurationChange).toHaveBeenCalledWith(180);

  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  fireEvent.click(screen.getByRole("button", { name: "界面" }));
  fireEvent.click(screen.getByRole("radio", { name: "左侧" }));
  expect(workspace).toHaveAttribute("data-dock-side", "left");
  fireEvent.click(screen.getByRole("button", { name: "关闭系统设置" }));

  openAiPanel();
  const sideResizer = screen.getByRole("separator", { name: "调整辅助区宽度" });
  fireEvent.keyDown(sideResizer, { key: "ArrowRight" });
  expect(sideResizer).toHaveAttribute("aria-valuenow", "356");

  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  fireEvent.click(screen.getByRole("button", { name: "界面" }));
  fireEvent.click(screen.getByRole("button", { name: "恢复默认布局" }));
  expect(workspace).toHaveAttribute("data-dock-side", "right");
  expect(sideResizer).toHaveAttribute("aria-valuenow", "340");
});

test("策略面板有自己的宽度：默认半个窗口，拖宽后不影响对话面板", () => {
  const values = props();
  render(<FuturesDecisionWorkspace {...values as any} />);
  const workspace = screen.getByTestId("futures-decision-workspace");
  openAiPanel();
  expect(screen.getByRole("separator", { name: "调整辅助区宽度" })).toHaveAttribute("aria-valuenow", "340");
  fireEvent.click(screen.getByRole("button", { name: "打开策略面板" }));
  const half = Math.min(Math.round(window.innerWidth / 2), Math.max(280, window.innerWidth - 58 - 480));
  const resizer = screen.getByRole("separator", { name: "调整辅助区宽度" });
  expect(resizer).toHaveAttribute("aria-valuenow", String(half));
  expect(workspace.style.getPropertyValue("--fdw-right-dock")).toBe(`${half}px`);
  fireEvent.keyDown(resizer, { key: "ArrowLeft" });
  expect(resizer).toHaveAttribute("aria-valuenow", String(Math.min(half + 16, Math.max(280, window.innerWidth - 58 - 480))));
  openAiPanel();
  expect(screen.getByRole("separator", { name: "调整辅助区宽度" })).toHaveAttribute("aria-valuenow", "340");
});

test("对话面板默认宽度随窗口、拖动最宽到半窗并保存；窗口变化时未保存的默认跟着重算，保存的宽度按新窗口截断", () => {
  vi.stubGlobal("innerWidth", 1440);
  const values = props();
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const workspace = screen.getByTestId("futures-decision-workspace");
  openAiPanel();
  const resizer = () => screen.getByRole("separator", { name: "调整辅助区宽度" });
  expect(resizer()).toHaveAttribute("aria-valuenow", "380");
  expect(resizer()).toHaveAttribute("aria-valuemax", "720");
  expect(workspace.style.getPropertyValue("--fdw-dock-max")).toBe("720px");
  expect(JSON.parse(localStorage.getItem("ai-trader-workspace-layout-v3")!).dockWidth).toBeNull();
  // Not saved: the default follows the window.
  act(() => { vi.stubGlobal("innerWidth", 2560); window.dispatchEvent(new Event("resize")); });
  expect(resizer()).toHaveAttribute("aria-valuenow", "640");
  act(() => { vi.stubGlobal("innerWidth", 1920); window.dispatchEvent(new Event("resize")); });
  expect(resizer()).toHaveAttribute("aria-valuenow", "480");
  // Dragged (keyboard steps of 16) up to half the window, and saved.
  for (let step = 0; step < 40; step += 1) fireEvent.keyDown(resizer(), { key: "ArrowLeft" });
  expect(resizer()).toHaveAttribute("aria-valuenow", "960");
  expect(JSON.parse(localStorage.getItem("ai-trader-workspace-layout-v3")!).dockWidth).toBe(960);
  // A narrower window shows at most its half; the saved width comes back when the window widens again.
  act(() => { vi.stubGlobal("innerWidth", 1440); window.dispatchEvent(new Event("resize")); });
  expect(resizer()).toHaveAttribute("aria-valuenow", "720");
  expect(JSON.parse(localStorage.getItem("ai-trader-workspace-layout-v3")!).dockWidth).toBe(960);
  act(() => { vi.stubGlobal("innerWidth", 1920); window.dispatchEvent(new Event("resize")); });
  expect(resizer()).toHaveAttribute("aria-valuenow", "960");
  view.unmount();
  // A reload keeps the saved width; 恢复默认布局 goes back to following the window.
  render(<FuturesDecisionWorkspace {...props() as any} />);
  openAiPanel();
  expect(resizer()).toHaveAttribute("aria-valuenow", "960");
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  fireEvent.click(screen.getByRole("button", { name: "界面" }));
  fireEvent.click(screen.getByRole("button", { name: "恢复默认布局" }));
  expect(resizer()).toHaveAttribute("aria-valuenow", "480");
  expect(JSON.parse(localStorage.getItem("ai-trader-workspace-layout-v3")!).dockWidth).toBeNull();
});

test("dockWidth 界面命令：在窗口上限内照设并回报，超过这个窗口的上限直接拒绝并说明上限", () => {
  vi.stubGlobal("innerWidth", 1920);
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  const apply = (changes: Record<string, unknown>) => control.mock.calls.at(-1)?.[3](changes as any);
  try {
    act(() => { apply({ panel: "ai" }); });
    expect(state()).toMatchObject({ panel: "ai", dockWidth: 480 });
    act(() => { apply({ dockWidth: 900 }); });
    expect(state()).toMatchObject({ dockWidth: 900 });
    expect(() => apply({ dockWidth: 1200 })).toThrow("这个窗口里辅助区最宽 960px");
    expect(() => apply({ presentation: { dockWidth: 1200, theme: "light" } })).toThrow("这个窗口里辅助区最宽 960px");
    expect(state()).toMatchObject({ dockWidth: 900 });
  } finally {
    view.unmount(); control.mockRestore();
  }
});

test("策略编辑卡打开时策略面板约占窗口四分之三、图表留四分之一；拖动只在这次编辑里有效，关掉编辑卡回到面板原来的宽度且不改存的宽度", () => {
  resetStrategyEditor();
  const values = props();
  render(<FuturesDecisionWorkspace {...values as any} />);
  const workspace = screen.getByTestId("futures-decision-workspace");
  fireEvent.click(screen.getByRole("button", { name: "打开策略面板" }));
  const resizer = () => screen.getByRole("separator", { name: "调整辅助区宽度" });
  fireEvent.keyDown(resizer(), { key: "ArrowLeft" });
  const own = resizer().getAttribute("aria-valuenow");
  const saved = localStorage.getItem("ai-trader-workspace-layout-v3");
  act(() => newDraft("blank"));
  const quarter = Math.min(Math.max(280, Math.round(window.innerWidth * 0.75) - 58), Math.max(280, window.innerWidth - 58 - 300));
  expect(resizer()).toHaveAttribute("aria-valuenow", String(quarter));
  expect(workspace.style.getPropertyValue("--fdw-right-dock")).toBe(`${quarter}px`);
  expect(workspace.style.getPropertyValue("--fdw-chart-min")).toBe("300px");
  fireEvent.keyDown(resizer(), { key: "ArrowRight" });
  expect(localStorage.getItem("ai-trader-workspace-layout-v3")).toBe(saved);
  act(() => discardDraft());
  expect(resizer()).toHaveAttribute("aria-valuenow", own);
  expect(workspace.style.getPropertyValue("--fdw-chart-min")).toBe("");
  expect(localStorage.getItem("ai-trader-workspace-layout-v3")).toBe(saved);
});

function selectCurrentPrice() {
  openOrderPanel();
  fireEvent.click(within(screen.getByRole("region", { name: "五档" })).getByRole("button", {name: /^最新/}));
  fireEvent.click(screen.getAllByRole("button", {name: "收起模拟下单"})[0]);
}

test("交易页默认专注图表，打开下单抽屉后方向选择不提交且确认按钮才授权", async () => {
  const values = props();
  render(<FuturesDecisionWorkspace {...values as any} />);

  expect(screen.getByTestId("futures-decision-workspace")).toHaveAttribute("data-right-dock", "closed");
  selectCurrentPrice();
  openOrderPanel();
  expect(screen.queryByLabelText("AI 对话")).not.toBeInTheDocument();
  expect(screen.getByLabelText("固定下单面板")).toBeVisible();
  const buy = await screen.findByRole("button", { name: "买多" });
  fireEvent.click(buy);
  expect(values.onManualSubmit).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ }));

  await waitFor(() => expect(values.onManualSubmit).toHaveBeenCalledTimes(1));
  expect(values.onManualSubmit).toHaveBeenCalledWith(expect.objectContaining({
    side: "buy_open",
    source: "three-key"
  }));
});

test("AI 与下单面板互斥；旧 panel: depth（价格阶梯已并入下单面板）打开下单面板，回执按下单面板判", () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  try {
    const workspace = screen.getByTestId("futures-decision-workspace");
    openAiPanel();
    expect(workspace).toHaveAttribute("data-right-dock", "ai");
    expect(screen.queryByRole("button", { name: /价格阶梯/ })).toBeNull();
    act(() => { control.mock.calls.at(-1)?.[3]({ panel: "depth" } as any); });
    expect(workspace).toHaveAttribute("data-right-dock", "order");
    expect(screen.queryByLabelText("AI 对话")).not.toBeInTheDocument();
    expect(within(screen.getByLabelText("固定下单面板")).getByRole("region", { name: "五档" })).toBeVisible();
    expect(state()).toMatchObject({ panel: "order", view: "trade" });
  } finally {
    view.unmount(); control.mockRestore();
  }
});

test("合约抽屉空目录：快照报 contractCatalog，contractCatalogRetry 与“重试”同一操作；抽屉没开或不能重试时拒绝", () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const values = props({ instruments: { ...instruments, items: [], count: 0, catalogStatus: "unavailable" } });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  const apply = (changes: Record<string, unknown>) => control.mock.calls.at(-1)?.[3](changes as any);
  try {
    expect(() => apply({ contractCatalogRetry: true })).toThrow("请先打开合约抽屉");
    act(() => { apply({ dialog: { name: "contract-drawer", open: true } }); });
    expect(state()).toMatchObject({ dialog: "contract-drawer", contractCatalog: { status: "empty", canRetry: true } });
    act(() => { apply({ contractCatalogRetry: true }); });
    expect(values.onRetryInstruments).toHaveBeenCalledOnce();
    view.rerender(<FuturesDecisionWorkspace {...{ ...values, instrumentsLoading: true } as any} />);
    expect(state()).toMatchObject({ contractCatalog: { status: "loading", canRetry: false } });
    expect(() => apply({ contractCatalogRetry: true })).toThrow("合约目录正在读取，不用重试");
    view.rerender(<FuturesDecisionWorkspace {...{ ...values, connection: "disconnected" } as any} />);
    expect(() => apply({ contractCatalogRetry: true })).toThrow("应用服务未连接，连上后会自动读取合约目录");
    expect(values.onRetryInstruments).toHaveBeenCalledOnce();
  } finally {
    view.unmount(); control.mockRestore();
  }
});

const depthQuote = {
  ...market.quote,
  askPrice2: 824.04, askVolume2: 30, askPrice3: 824.06, askVolume3: 27, askPrice4: 824.08, askVolume4: 22, askPrice5: 824.10, askVolume5: 18,
  bidPrice2: 823.96, bidVolume2: 39, bidPrice3: 823.94, bidVolume3: 31
  // 买四、买五缺失。
};

test("下单面板：有效五档在下单票上方，点价填入，缺档隐藏", () => {
  const values = props({ market: { ...market, quote: depthQuote } });
  render(<FuturesDecisionWorkspace {...values as any} />);
  openOrderPanel();
  const panel = screen.getByLabelText("固定下单面板");
  const depth = within(panel).getByRole("region", { name: "五档" });
  expect(depth.compareDocumentPosition(within(panel).getByLabelText("三键模拟交易台")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  fireEvent.click(within(depth).getByRole("button", { name: "展开五档" }));
  expect(depth.querySelectorAll(".fdw-ladder-rows button")).toHaveLength(9);
  expect(within(depth).queryByRole("button", { name: /^买四/ })).not.toBeInTheDocument();
  expect(within(depth).queryByRole("button", { name: /^买五/ })).not.toBeInTheDocument();
  // A level fills the ticket price without creating a draft.
  fireEvent.click(within(depth).getByRole("button", { name: /^卖三/ }));
  expect(screen.getByLabelText("模拟委托价")).toHaveValue(824.06);
  expect(within(depth).getByRole("button", { name: /^卖三/ })).toHaveClass("selected");
  expect(screen.queryByTestId("ticket-draft")).not.toBeInTheDocument();
  expect(values.onManualSubmit).not.toHaveBeenCalled();
});

test("点档位和 selectedPrice 共用只填价入口：对手价切限价，同一价可重填，不改方向或提交", async () => {
  preferencesRuntime({ trade: { ...DEFAULT_PREFERENCES.trade, quickOrder: true, defaultVolume: 2 } });
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const values = props({ market: { ...market, quote: depthQuote }, runtimeBaseUrl: "https://runtime.example.test" });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  try {
    openOrderPanel();
    fireEvent.click(screen.getByRole("button", {name:"卖空"}));
    fireEvent.click(screen.getByRole("button", {name:"对手价"}));
    const depth = () => within(screen.getByLabelText("五档"));
    fireEvent.click(depth().getByRole("button", {name:/^卖一/}));
    expect(screen.getByLabelText("模拟委托价")).toHaveValue(824.02);
    expect(screen.getByRole("button", {name:"卖空"})).toHaveClass("active");
    fireEvent.click(screen.getByRole("button", {name:"最新价"}));
    fireEvent.click(depth().getByRole("button", {name:/^卖一/}));
    expect(screen.getByLabelText("模拟委托价")).toHaveValue(824.02);
    fireEvent.click(depth().getByRole("button", {name:"展开五档"}));
    fireEvent.click(screen.getByRole("button", {name:"对手价"}));
    fireEvent.click(depth().getByRole("button", {name:/^买三/}));
    expect(screen.getByLabelText("模拟委托价")).toHaveValue(823.94);
    act(() => { control.mock.calls.at(-1)?.[3]({selectedPrice: 824.06} as any); });
    expect(screen.getByLabelText("模拟委托价")).toHaveValue(824.06);
    const state = control.mock.calls.at(-1)?.[2] as any;
    expect(state).toMatchObject({selectedPrice:824.06,ticketPrice:824.06,ticketPriceMode:"limit"});
    expect(screen.queryByTestId("ticket-draft")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", {name:"确认快捷下单"})).not.toBeInTheDocument();
    expect(values.onManualSubmit).not.toHaveBeenCalled();
  } finally { view.unmount();control.mockRestore(); }
});

test("下单面板：仅一档时只显示三格，五档 disclosure 仍可记忆与命令控制", () => {
  const first = render(<FuturesDecisionWorkspace {...props() as any} />);
  openOrderPanel();
  expect(screen.getByLabelText("五档").querySelectorAll(".fdw-order-topline button")).toHaveLength(3);
  expect(screen.queryByRole("button", {name: "展开五档"})).not.toBeInTheDocument();
  first.unmount();
  const values = props({ market: { ...market, quote: depthQuote } });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  openOrderPanel();
  expect(screen.getByRole("button", {name: "展开五档"})).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(screen.getByRole("button", {name: "展开五档"}));
  expect(uiSurface("disclosure:order-depth")?.state).toBe(true);
  expect(localStorage.getItem("ai-trader:order-depth-open")).toBe("1");
  view.unmount();
  render(<FuturesDecisionWorkspace {...values as any} />);
  openOrderPanel();
  expect(screen.getByRole("button", {name: "收起五档"})).toHaveAttribute("aria-expanded", "true");
  act(() => { uiSurface("disclosure:order-depth")!.apply!(false); });
  expect(screen.getByRole("button", {name: "展开五档"})).toHaveAttribute("aria-expanded", "false");
});

test("下单面板：本合约持仓只列当前合约，带平仓/保护/AI 与本合约在途委托", () => {
  const working = (symbol: string, id: string) => ({
    clientOrderId: id, proposalId: null, displayStatus: "已报", submitPhase: "CONFIRMED", cancelPhase: "NONE", remoteStatus: "ALIVE",
    request: { symbol, direction: "BUY", offset: "OPEN", limitPrice: 820, volume: 1 }
  });
  const values = props({
    account: { ...account, positions: [...account.positions, { ...account.positions[0], symbol: "SHFE.ag2612", unrealizedPnl: 750 }] },
    simulationOrders: [working("SHFE.au2612", "AIT-AU-1"), working("SHFE.ag2612", "AIT-AG-1")]
  });
  render(<FuturesDecisionWorkspace {...values as any} />);
  openOrderPanel();
  const holdings = within(screen.getByLabelText("固定下单面板")).getByRole("region", { name: "本合约持仓" });
  expect(holdings.querySelectorAll("[data-position]")).toHaveLength(1);
  expect(holdings.querySelector("[data-position]")).toHaveAttribute("data-position", expect.stringContaining("SHFE.au2612"));
  expect(within(holdings).getByRole("button", { name: "给 SHFE.au2612 多仓挂保护" })).toBeVisible();
  expect(within(holdings).getByRole("button", { name: "AI：沪金2612 多 2 手" })).toBeVisible();
  expect(holdings.querySelectorAll("[data-order-id]")).toHaveLength(1);
  fireEvent.click(within(holdings).getByRole("button", { name: "撤单" }));
  expect(values.onOrderAction).toHaveBeenCalledWith(expect.objectContaining({ clientOrderId: "AIT-AU-1" }), "cancel");
  fireEvent.click(within(holdings).getByRole("button", { name: "平仓 SHFE.au2612 多仓" }));
  expect(screen.getByRole("button", { name: "平仓" })).toHaveAttribute("aria-pressed", "true");
});

test("下单面板：本合约没有持仓时一行灰字", () => {
  render(<FuturesDecisionWorkspace {...props({ account: { ...account, positions: [{ ...account.positions[0], symbol: "SHFE.ag2612" }] } }) as any} />);
  openOrderPanel();
  const holdings = within(screen.getByLabelText("固定下单面板")).getByRole("region", { name: "本合约持仓" });
  expect(within(holdings).getByText("本合约无持仓")).toBeVisible();
  expect(holdings.querySelector("[data-position]")).toBeNull();
});

test("向 AI 提问时附带当前期货工作区上下文但不改变用户问题", async () => {
  const values = props();
  render(<FuturesDecisionWorkspace {...values as any} />);
  openAiPanel();

  fireEvent.change(screen.getByRole("textbox", { name: "向 AI 决策流提问" }), {
    target: { value: "当前合约怎么看？" }
  });
  fireEvent.click(screen.getByRole("button", { name: "发送给AI" }));

  await waitFor(() => expect(values.onPrompt).toHaveBeenCalledTimes(1));
  expect(values.onPrompt).toHaveBeenCalledWith(
    "当前合约怎么看？",
    expect.objectContaining({
      symbol: "SHFE.au2612",
      instrumentName: "沪金2612",
      durationSeconds: 300,
      dataStatus: "normal",
      marketSessionStatus: "trading",
      view: "trade",
      rightDock: "ai",
      selectedPosition: expect.objectContaining({
        symbol: "SHFE.au2612",
        direction: "long",
        unrealizedPnl: 2340
      }),
      activeOrders: [],
      recentTrades: [],
      recentActions: [],
      positions: expect.arrayContaining([
        expect.objectContaining({ symbol: "SHFE.au2612", direction: "long" })
      ])
    })
  );
});

test("AI 上下文携带当前订单草稿和本次会话的相关操作而不把它当成交", async () => {
  const values = props();
  render(<FuturesDecisionWorkspace {...values as any} />);

  fireEvent.keyDown(window, { key: "b", altKey: true });
  expect(await screen.findByLabelText("固定下单面板")).toBeVisible();
  expect(screen.queryByLabelText("AI 对话")).not.toBeInTheDocument();
  openAiPanel();
  expect(screen.getByLabelText("AI 对话")).toBeVisible();
  expect(screen.queryByLabelText("固定下单面板")).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "向 AI 决策流提问" }), {
    target: { value: "你对我这一手操作怎么看？" }
  });
  fireEvent.click(screen.getByRole("button", { name: "发送给AI" }));

  await waitFor(() => expect(values.onPrompt).toHaveBeenCalledTimes(1));
  expect(values.onPrompt).toHaveBeenCalledWith(
    "你对我这一手操作怎么看？",
    expect.objectContaining({
      rightDock: "ai",
      orderDraft: expect.objectContaining({
        side: "buy_open",
        volume: 1,
        source: "context-menu"
      }),
      recentActions: expect.arrayContaining([
        expect.objectContaining({ action: "order_draft_created", side: "buy_open", volume: 1 })
      ]),
      recentTrades: []
    })
  );
});

test("行情不可用时方向仍可编辑但确认下单与平仓失效", async () => {
  const values = props({ simulationEnabled: false });
  render(<FuturesDecisionWorkspace {...values as any} />);
  openOrderPanel();

  expect(await screen.findByRole("button", { name: "买多" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "卖空" })).toBeEnabled();
  expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", {name:/^平仓$/}));
  expect(screen.getByRole("button", { name: /(卖出平仓|买入平仓)/ })).toBeDisabled();
});

test("目标持仓页：三模式切换、本地差额预览、生成方案卡走确认卡而不是直接下单", async () => {
  const values = props();
  render(<FuturesDecisionWorkspace {...values as any} />);
  openOrderPanel();
  await screen.findByRole("button", { name: "买多" });
  // The account holds one au long 2: entering the page seeds the target at the current net (+2).
  fireEvent.click(screen.getByRole("button", { name: "目标持仓" }));
  expect(screen.getByRole("button", { name: "目标持仓" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByTestId("target-plan-summary")).toHaveTextContent("目标 +2 手，当前 +2 手 → 已是目标持仓，不用下单");
  expect(screen.getByTestId("generate-target-card")).toBeDisabled();
  expect(screen.getByTestId("generate-target-card")).toHaveTextContent("已是目标持仓");
  // Turn the stepper to +3: the preview shows the one-leg difference and the button arms.
  fireEvent.click(screen.getByRole("button", { name: "目标净持仓加一" }));
  expect(screen.getByTestId("target-plan-summary")).toHaveTextContent("目标 +3 手，当前 +2 手 → 买开 1 手");
  fireEvent.click(screen.getByTestId("generate-target-card"));
  expect(values.onTargetPosition).toHaveBeenCalledWith({ symbol: "SHFE.au2612", targetNet: 3, priceIntent: "passive" });
  expect(values.onManualSubmit).not.toHaveBeenCalled();
  // A reverse target previews two legs; the price intent follows the 对手价 toggle.
  fireEvent.change(screen.getByTestId("target-net-input"), { target: { value: "-2" } });
  expect(screen.getByTestId("target-plan-summary")).toHaveTextContent("目标 -2 手，当前 +2 手 → 平掉全部 2 手，再卖开 2 手");
  fireEvent.click(screen.getByRole("button", { name: "对手价" }));
  fireEvent.click(screen.getByTestId("generate-target-card"));
  expect(values.onTargetPosition).lastCalledWith({ symbol: "SHFE.au2612", targetNet: -2, priceIntent: "aggressive" });
  // Back to 开仓: the ordinary ticket is untouched.
  fireEvent.click(screen.getByRole("button", { name: /^开仓$/ }));
  expect(screen.queryByTestId("target-plan-summary")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeEnabled();
});

test("休市仍显示历史 K 线和复盘入口，但禁止任何三键提交", async () => {
  const closedMarket: MarketWorkbenchSnapshot = {
    ...market,
    ageSeconds: 4 * 60 * 60,
    marketSessionStatus: "closed",
    quote: {
      ...market.quote,
      marketSessionStatus: "closed"
    }
  };
  const values = props({ market: closedMarket, simulationEnabled: false });
  render(<FuturesDecisionWorkspace {...values as any} />);

  expect(screen.getByRole("button", { name: "休市，查看交易时间" })).toBeVisible();
  expect(screen.getByLabelText("K 线图")).toBeVisible();
  openOrderPanel();
  expect(await screen.findByRole("button", { name: "买多" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "卖空" })).toBeEnabled();
  expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", {name:/^平仓$/}));
  expect(screen.getByRole("button", { name: /(卖出平仓|买入平仓)/ })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "复盘" }));
  // 第八批 (2026-10-08): the built-in 示例趋势策略回测 left the 复盘 toolbar.
  expect(screen.queryByRole("button", { name: "示例趋势策略回测" })).toBeNull();
  expect(screen.queryByText("模拟下单", { exact: true })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  const settings = screen.getByRole("dialog", { name: "设置" });
  fireEvent.click(within(settings).getByRole("button", { name: "交易" }));
  expect(within(settings).getByText("模拟网关").closest(".app-setting-row")).toHaveTextContent("已连接");
  expect(within(settings).getByText("休市，暂不可提交")).toBeVisible();
});

/** 网页新版本: this page is an older pipeline build; version.json answers `deployed`; reload is a spy. */
function olderWebBuild(deployed = "1685e2b0c0ffee0000000000deadbeef1234") {
  const previousCommit = CLIENT_BUILD.commit;
  CLIENT_BUILD.commit = "f8f50e5c0b9d7a2e4d6c8a1b3e5f7d9c0a2b4e6f";
  sessionStorage.clear();
  const fetchMock = vi.fn(async (_url: unknown) => ({ ok: true, json: async () => ({ version: "1.7.0", commit: deployed }) }));
  vi.stubGlobal("fetch", fetchMock);
  const reload = vi.spyOn(webPage, "reload").mockImplementation(() => undefined);
  return { fetchMock, reload, restore: () => { reload.mockRestore(); CLIENT_BUILD.commit = previousCommit; resetClientCompat(); sessionStorage.clear(); } };
}
const backToForeground = () => act(() => { document.dispatchEvent(new Event("visibilitychange")); });

test("网页新版本：输入框有字、按住说话中、通话中时不自动刷新而显示提示条，点刷新才刷新；空闲回到前台自动刷新；快照 webUpdate", async () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const build = olderWebBuild();
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  openAiPanel();
  fireEvent.change(screen.getByRole("textbox", { name: "向 AI 决策流提问" }), { target: { value: "螺纹怎么看" } });
  const cancel = watchWebBuild();
  try {
    await waitFor(() => expect(build.fetchMock).toHaveBeenCalledTimes(1));
    const bar = await screen.findByText("新版本已就绪");
    expect(build.reload).not.toHaveBeenCalled();
    expect(control.mock.calls.at(-1)?.[4].webUpdate).toEqual({ available: true, banner: true });

    // Recording, then in a call (the input emptied): still busy.
    fireEvent.change(screen.getByRole("textbox", { name: "向 AI 决策流提问" }), { target: { value: "" } });
    const ptt = vi.spyOn(pushToTalkStore, "snapshot").mockReturnValue({ state: "recording", level: 0, partial: "" });
    backToForeground();
    ptt.mockRestore();
    const call = vi.spyOn(voiceCallStore, "inCall").mockReturnValue(true);
    backToForeground();
    expect(build.reload).not.toHaveBeenCalled();

    fireEvent.click(within(bar.closest(".web-update-bar") as HTMLElement).getByRole("button", { name: "刷新" }));
    expect(build.reload).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "关闭新版本提示" }));
    expect(screen.queryByText("新版本已就绪")).toBeNull();
    expect(control.mock.calls.at(-1)?.[4].webUpdate).toEqual({ available: true, banner: false });

    call.mockRestore();
    backToForeground(); // idle now: the page reloads itself
    expect(build.reload).toHaveBeenCalledTimes(2);
  } finally {
    cancel(); view.unmount(); control.mockRestore(); build.restore();
  }
});

test("网页新版本：手机下单页打开时不自动刷新、提示条在内容顶部；我的页底部版本行检查更新", async () => {
  stubPhone(true);
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const build = olderWebBuild();
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  act(() => { control.mock.calls.at(-1)?.[3]({ mobilePage: "order" } as any); });
  const cancel = watchWebBuild();
  try {
    await waitFor(() => expect(build.fetchMock).toHaveBeenCalledTimes(1));
    const bar = (await screen.findByText("新版本已就绪")).closest(".web-update-bar");
    expect(bar?.parentElement).toBe(screen.getByTestId("mobile-workspace"));
    backToForeground();
    expect(build.reload).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "关闭下单" }));
    fireEvent.click(tabButton("我的"));
    const version = screen.getByTestId("mobile-me-version");
    expect(version).toHaveTextContent(`版本 ${CLIENT_BUILD.version} · f8f50e5`);
    fireEvent.click(within(version).getByRole("button", { name: "检查更新" }));
    await waitFor(() => expect(build.reload).toHaveBeenCalledTimes(1));
  } finally {
    cancel(); view.unmount(); control.mockRestore(); build.restore();
  }
});

test("设置 → 关于：检查更新无新版提示已是最新", async () => {
  const build = olderWebBuild("f8f50e5c0b9d7a2e4d6c8a1b3e5f7d9c0a2b4e6f"); // version.json names this very build
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  try {
    fireEvent.click(screen.getByRole("button", { name: "设置" }));
    fireEvent.click(screen.getByRole("button", { name: "关于" }));
    const row = within(screen.getByRole("dialog", { name: "设置" })).getByText("软件版本").closest(".app-setting-row") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "检查更新" }));
    expect(await within(row).findByText("已是最新")).toBeVisible();
    expect(build.reload).not.toHaveBeenCalled();
  } finally {
    view.unmount(); build.restore();
  }
});

test("设置页发现更新后可以安装，并在安装期间阻止重复操作", () => {
  const install = vi.fn(async () => undefined);
  const check = vi.fn(async () => undefined);
  vi.stubGlobal("__TAURI_INTERNALS__", {});  // the update row is desktop-only; the web build has 检查更新 beside 软件版本
  const values = props({
    updater: {
      phase: "available" as const,
      info: { currentVersion: "1.1.0", available: true, version: "1.1.1" },
      check,
      install
    }
  });
  const { rerender } = render(<FuturesDecisionWorkspace {...values as any} />);

  fireEvent.click(screen.getByTitle("设置"));
  fireEvent.click(screen.getByRole("button", { name: "关于" }));
  fireEvent.click(screen.getByRole("button", { name: "下载并安装 1.1.1" }));
  expect(install).toHaveBeenCalledTimes(1);

  rerender(<FuturesDecisionWorkspace {...{
    ...values,
    updater: {
      ...values.updater,
      phase: "downloading" as const,
      progress: { stage: "downloading" as const, downloadedBytes: 46, totalBytes: 100, percent: 46 }
    }
  } as any} />);
  expect(screen.getByRole("button", { name: "正在下载更新…" })).toBeDisabled();
  expect(screen.getByRole("progressbar", { name: "正在下载 46%" })).toHaveAttribute("aria-valuenow", "46");
  expect(screen.getByRole("button", { name: "重新检查更新" })).toBeDisabled();
  expect(check).not.toHaveBeenCalled();
});

test("字体大小和界面密度分开选择并交给应用层持久化", () => {
  const values = props();
  render(<FuturesDecisionWorkspace {...values as any} />);

  fireEvent.click(screen.getByTitle("设置"));
  const settings = screen.getByRole("dialog", { name: "设置" });
  fireEvent.click(within(settings).getByRole("button", { name: "界面" }));
  const large = within(settings).getByRole("radio", { name: "大字 110%" });
  const extraLarge = within(settings).getByRole("radio", { name: "特大 120%" });
  const standardDensity = within(settings).getByRole("radio", { name: "标准" });
  const compactDensity = within(settings).getByRole("radio", { name: "紧凑" });
  expect(large).toHaveAttribute("aria-checked", "true");
  expect(extraLarge).toHaveAttribute("aria-checked", "false");
  expect(standardDensity).toHaveAttribute("aria-checked", "true");
  expect(compactDensity).toHaveAttribute("aria-checked", "false");

  fireEvent.click(extraLarge);
  fireEvent.click(compactDensity);
  expect(values.onFontScaleChange).toHaveBeenCalledWith("extra-large");
  expect(values.onInterfaceDensityChange).toHaveBeenCalledWith("compact");
  fireEvent.click(within(settings).getByRole("button", { name: "快捷键" }));
  expect(within(settings).getByText("恢复默认大小")).toBeVisible();
});

test("设置 → 图表：应用服务未连接时置灰的账户偏好写明原因，本机显示开关照常可用（#712 B5）", () => {
  render(<FuturesDecisionWorkspace {...props() as any} />);
  fireEvent.click(screen.getByTitle("设置"));
  const settings = screen.getByRole("dialog", { name: "设置" });
  fireEvent.click(within(settings).getByRole("button", { name: "图表" }));
  for (const label of ["默认周期", "K 线样式", "涨跌颜色"]) {
    const row = within(settings).getByText(label, { selector: ".app-setting-name span" }).closest(".app-setting-row") as HTMLElement;
    expect(within(row).getByText("应用服务未连接，连接后可修改")).toBeVisible();
  }
  const annotations = within(settings).getByRole("region", { name: "AI 标注" });
  expect(within(annotations).getByText("应用服务未连接，连接后可修改")).toBeVisible();
  expect(within(annotations).getByRole("switch", { name: "默认显示 AI 标注" })).toBeDisabled();
});

test("设置 → 手续费页承载费率编辑，面板可通过事件直接打开该页", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ source: "custom", default: { mode: "per_lot", open: 10, closeToday: 20, closeYesterday: 30 }, overrides: [] }), { status: 200, headers: { "Content-Type": "application/json" } })));
  render(<FuturesDecisionWorkspace {...props({ rootBaseUrl: "http://runtime" }) as any} />);
  act(() => { window.dispatchEvent(new CustomEvent("ai-trader-open-settings", { detail: "fees" })); });
  const settings = screen.getByRole("dialog", { name: "设置" });
  // The 1.6.5 "fees" page maps onto 高级交易设置 (trade-advanced), where the fee editor lives;
  // the nav keeps highlighting its parent 交易.
  expect(within(settings).getByRole("button", { name: "交易" })).toHaveAttribute("aria-pressed", "true");
  expect(await within(settings).findByRole("button", { name: /手续费.*开仓 10.*平今 20.*平昨 30/ })).toBeVisible();
  vi.unstubAllGlobals();
});

test("界面主题支持深色、浅色和跟随系统并应用到整个工作台", () => {
  const listeners: Array<(event: MediaQueryListEvent) => void> = [];
  let systemDark = false;
  // Only the colour-scheme query is simulated; the phone-layout query (手机版) stays a desktop.
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
    get matches() { return query === "(prefers-color-scheme: dark)" && systemDark; },
    media: query,
    onchange: null,
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => { if (query === "(prefers-color-scheme: dark)") listeners.push(listener); },
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn()
  })));
  render(<FuturesDecisionWorkspace {...props() as any} />);
  const workspace = screen.getByTestId("futures-decision-workspace");

  fireEvent.click(screen.getByTitle("设置"));
  const settings = screen.getByRole("dialog", { name: "设置" });
  fireEvent.click(within(settings).getByRole("button", { name: "界面" }));
  expect(within(settings).getByRole("radio", { name: "深色" })).toHaveAttribute("aria-checked", "true");
  fireEvent.click(within(settings).getByRole("radio", { name: "浅色" }));
  expect(workspace).toHaveClass("app-theme-light");
  fireEvent.click(within(settings).getByRole("radio", { name: "跟随系统" }));
  expect(workspace).toHaveAttribute("data-theme-preference", "system");
  expect(workspace).toHaveClass("app-theme-light");

  systemDark = true;
  act(() => listeners.forEach((listener) => listener({ matches: true } as MediaQueryListEvent)));
  expect(workspace).toHaveClass("app-theme-dark");
});

test("复盘页回读已持久化的委托链且未知状态只提供查询", async () => {
  const order = {
    clientOrderId: "AIT-UNKNOWN-1",
    proposalId: "proposal-1",
    displayStatus: "结果未知",
    submitPhase: "UNKNOWN",
    cancelPhase: "NONE",
    remoteStatus: "UNKNOWN",
    request: {
      symbol: "SHFE.au2612",
      direction: "SELL",
      offset: "CLOSETODAY",
      limitPrice: 824,
      volume: 1
    }
  };
  const values = props({ simulationOrders: [order] });
  render(<FuturesDecisionWorkspace {...values as any} />);

  fireEvent.click(screen.getByRole("button", { name: "复盘" }));
  const review = screen.getByText("本地委托链").closest("section")!;
  expect(review).toBeVisible();
  expect(within(review).getByText("结果未知")).toBeVisible();
  const query = within(review).getByRole("button", { name: "查询" });
  fireEvent.click(query);
  expect(values.onOrderAction).toHaveBeenCalledWith(order, "query");
  expect(within(review).queryByRole("button", { name: "撤单" })).not.toBeInTheDocument();
});

test("只有真实选中持仓后智能平仓才可执行", async () => {
  const twoSided = {
    ...account,
    positions: [
      ...account.positions,
      { ...account.positions[0], direction: "short" as const, unrealizedPnl: -100 }
    ]
  };
  const values = props({ account: twoSided });
  render(<FuturesDecisionWorkspace {...values as any} />);
  selectCurrentPrice();
  openOrderPanel();
  fireEvent.click(screen.getByRole("button", {name:/^平仓$/}));
  expect(await screen.findByRole("button", { name: /(卖出平仓|买入平仓)/ })).toBeDisabled();
  // Two sides on this contract: the quick row offers 平多 / 平空, the ticket needs a real selection.
  expect(screen.getByRole("button", { name: "一键平仓" })).toBeEnabled();
  // The quick row sits under the ticket's own confirm button, not next to the side choice.
  const ticket = screen.getByRole("region", { name: "三键模拟交易台" });
  expect(ticket.compareDocumentPosition(screen.getByTestId("quick-row")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  openPositionsPanel();
  const positions = screen.getByRole("region", { name: "持仓与委托" });
  fireEvent.click(within(positions).getByRole("button", { name: "平仓 SHFE.au2612 空仓" }));
  const close = await screen.findByRole("button", { name: /(卖出平仓|买入平仓)/ });
  expect(close).toBeEnabled();
  expect(close).toHaveTextContent("买入平仓 2 手");
  expect(screen.getByLabelText("模拟委托手数")).toHaveValue(2);
  expect(values.onManualSubmit).not.toHaveBeenCalled();
});

test("右键菜单只服务图表：主图菜单没有下单项，下单仍在下单面板；左键选价只填草稿", () => {
  const values = props();
  values.onManualSubmit = vi.fn(async () => true) as any;
  render(<FuturesDecisionWorkspace {...values as any} />);
  selectCurrentPrice();
  expect(values.onManualSubmit).not.toHaveBeenCalled();
  fireEvent.contextMenu(screen.getByLabelText("K 线图"), {clientY:100});
  const menu = screen.getByRole("menu", { name: "图表菜单" });
  expect(menu).toHaveTextContent("主图 · 821.50（光标价）");
  for (const gone of ["买入开仓", "卖出开仓", "仅填入草稿价", "图表与下单说明"]) expect(within(menu).queryByRole("menuitem", { name: gone })).toBeNull();
  expect(within(menu).queryByRole("menuitem", { name: /智能平/ })).toBeNull();
  for (const item of ["周期", "主图指标", "添加副图", "图表设置…", "让 AI 分析这个价", "让 AI 讲讲这一段", "清空图上标记"]) {
    expect(within(menu).getByRole("menuitem", { name: new RegExp(`^${item.replace(/[…?]/g, ".")}`) })).toBeInTheDocument();
  }
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("menu", { name: "图表菜单" })).toBeNull();
  // Ordering stays where it was: the order panel's draft (Alt+B) and its confirmation.
  fireEvent.keyDown(window, {key:"b", altKey:true});
  expect(screen.getByLabelText("模拟委托手数")).toBeInTheDocument();
  expect(values.onManualSubmit).not.toHaveBeenCalled();
});

test("快捷下单免确认打开后，快捷行直接提交对手价委托", async () => {
  localStorage.setItem("ai-trader:quick-order-skip-confirm:scope-a", "1");
  try {
    const values = props({ preferencesScope: "scope-a" });
    values.onManualSubmit = vi.fn(async () => true) as any;
    render(<FuturesDecisionWorkspace {...values as any} />);
    selectCurrentPrice();
    openOrderPanel();
    fireEvent.change(screen.getByLabelText("模拟委托手数"), {target:{value:"3"}});
    fireEvent.click(screen.getByRole("button", { name: "快捷手数 2" }));
    fireEvent.click(screen.getByRole("button", { name: "一键买多" }));
    await waitFor(() => expect(values.onManualSubmit).toHaveBeenCalledWith({side:"buy_open", volume:2, priceIntent:"aggressive", source:"three-key"}));
    // One long position on this contract: the smart close closes it all at the opponent price.
    fireEvent.click(screen.getByRole("button", { name: "一键平仓" }));
    await waitFor(() => expect(values.onManualSubmit).toHaveBeenCalledWith({side:"sell_close", volume:2, priceIntent:"aggressive", source:"three-key"}));
  } finally {
    localStorage.removeItem("ai-trader:quick-order-skip-confirm:scope-a");
  }
});

test("快捷下单免确认按用户分键：换到另一个账号不继承，原账号回来仍保留；游客不跳过；旧全局键被删除不继承", async () => {
  localStorage.setItem("ai-trader:quick-order-skip-confirm", "1");
  localStorage.setItem("ai-trader:quick-order-skip-confirm:scope-a", "1");
  const values = props({ preferencesScope: "scope-a" });
  values.onManualSubmit = vi.fn(async () => true) as any;
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  selectCurrentPrice();
  openOrderPanel();
  expect(localStorage.getItem("ai-trader:quick-order-skip-confirm")).toBeNull();
  // A has skip on: the quick row submits straight away.
  fireEvent.click(screen.getByRole("button", { name: /^一键买多/ }));
  await waitFor(() => expect(values.onManualSubmit).toHaveBeenCalledTimes(1));
  // B signs in on the same browser: confirmation dialog, nothing is sent.
  view.rerender(<FuturesDecisionWorkspace {...props({ preferencesScope: "scope-b", onManualSubmit: values.onManualSubmit }) as any} />);
  fireEvent.click(screen.getByRole("button", { name: /^一键买多/ }));
  expect(screen.getByRole("dialog", { name: /确认/ })).toBeInTheDocument();
  expect(values.onManualSubmit).toHaveBeenCalledTimes(1);
  // No known user (guest / signed out): never skip either.
  view.rerender(<FuturesDecisionWorkspace {...props({ preferencesScope: undefined, onManualSubmit: values.onManualSubmit }) as any} />);
  fireEvent.click(screen.getByRole("button", { name: /^一键买多/ }));
  expect(values.onManualSubmit).toHaveBeenCalledTimes(1);
  // A comes back: their own setting is still there.
  view.rerender(<FuturesDecisionWorkspace {...props({ preferencesScope: "scope-a", onManualSubmit: values.onManualSubmit }) as any} />);
  fireEvent.click(screen.getByRole("button", { name: /^一键买多/ }));
  await waitFor(() => expect(values.onManualSubmit).toHaveBeenCalledTimes(2));
  view.unmount();
});

test("副图上右键是副图自己的菜单（换成 / 参数 / 上移下移 / 添加 / 关闭），关闭副图写回偏好", async () => {
  const values = props();
  const view = render(<FuturesDecisionWorkspace {...values as any}/>);
  fireEvent.contextMenu(view.container.querySelectorAll(".fdw-chart-canvas")[1], {clientX: 40, clientY: 20});
  const menu = screen.getByRole("menu", { name: "图表菜单" });
  expect(menu).toHaveTextContent(/^副图 · KD/);
  for (const item of ["换成", "参数设置…", "上移", "下移", "添加副图", "关闭这个副图"]) {
    expect(within(menu).getByRole("menuitem", { name: new RegExp(`^${item.replace(/…/g, ".")}`) })).toBeInTheDocument();
  }
  expect(within(menu).getByRole("menuitem", { name: "上移" })).toBeDisabled();   // KD is the first pane
  expect(within(menu).getByRole("menuitem", { name: "关闭这个副图" })).toHaveClass("danger");
  expect(within(menu).queryByRole("menuitem", { name: /开仓|平/ })).toBeNull();
  expect(values.onManualSubmit).not.toHaveBeenCalled();
});

test("#712 B6 未连接应用服务时右键菜单里的画线置灰并说明原因，而不是整项消失", () => {
  render(<FuturesDecisionWorkspace {...props() as any} />);
  fireEvent.contextMenu(screen.getByLabelText("K 线图"), {clientY:100});
  const draw = within(screen.getByRole("menu", { name: "图表菜单" })).getByRole("menuitem", { name: /^画线/ });
  expect(draw).toBeDisabled();
  expect(draw).toHaveTextContent("应用服务未连接，连上后可画线");
});

test("清除候选价后行情刷新不会恢复候选线，也不会提交订单", () => {
  const values = props();
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  expect(view.container.querySelector(".fdw-chart-caption")).toBeNull();
  expect(view.container.querySelector(".fdw-selected-price")).toBeNull();
  selectCurrentPrice();
  expect(view.container.querySelector(".fdw-selected-price")).not.toBeNull();
  // 2026-09-30：右键菜单不再有“撤销选价”，清除走候选价徽标上的 ×。
  fireEvent.contextMenu(screen.getByLabelText("K 线图"), {clientY:100});
  expect(within(screen.getByRole("menu")).queryByRole("menuitem", { name: "撤销选价" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "清除候选价" }));
  expect(view.container.querySelector(".fdw-selected-price")).toBeNull();
  view.rerender(<FuturesDecisionWorkspace {...{ ...values, market: { ...market, quote: { ...market.quote, lastPrice: 825 } } } as any} />);
  expect(view.container.querySelector(".fdw-selected-price")).toBeNull();
  expect(values.onManualSubmit).not.toHaveBeenCalled();
});

test("SettingsLink 进二级页并能返回：导航高亮留在上级页", () => {
  render(<FuturesDecisionWorkspace {...props() as any} />);
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  const settings = screen.getByRole("dialog", { name: "设置" });
  // The parent page's link row opens the second-level page.
  fireEvent.click(within(settings).getByRole("button", { name: "交易" }));
  fireEvent.click(within(settings).getByRole("button", { name: "打开高级交易设置" }));
  expect(screen.getByRole("region", { name: "下单细节" })).toBeVisible();
  expect(screen.getByRole("region", { name: "执行限制" })).toBeVisible();
  // The second-level page has no nav entry of its own; the nav keeps highlighting 交易.
  expect(within(settings).getByRole("button", { name: "交易" })).toHaveAttribute("aria-pressed", "true");
  expect(within(settings).queryByRole("button", { name: "高级交易设置" })).toBeNull();
  // The back button returns to the parent page's own rows (下单细节 rows stay on the second level).
  fireEvent.click(within(settings).getByRole("button", { name: "返回交易" }));
  expect(screen.queryByRole("region", { name: "下单细节" })).toBeNull();
  expect(screen.queryByRole("region", { name: "执行限制" })).toBeNull();
  expect(within(settings).queryByRole("switch", { name: "语音下单免口头确认" })).toBeNull();
  expect(within(settings).getByRole("switch", { name: "快捷下单免确认" })).toBeVisible();
});

test("还没识别出登录用户（连接/重连中）时快捷下单免确认禁用并说明原因（#782）", () => {
  const view = render(<FuturesDecisionWorkspace {...props({ preferencesScope: undefined }) as any} />);
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  const settings = screen.getByRole("dialog", { name: "设置" });
  fireEvent.click(within(settings).getByRole("button", { name: "交易" }));
  expect(within(settings).getByRole("switch", { name: "快捷下单免确认" })).toBeDisabled();
  expect(settings).toHaveTextContent(/连接后可修改|连上后可修改/);
  view.rerender(<FuturesDecisionWorkspace {...props({ preferencesScope: "scope-a" }) as any} />);
  expect(within(settings).getByRole("switch", { name: "快捷下单免确认" })).toBeEnabled();
});

test("右键清空图上标记后成交标记不再显示", async () => {
  // One fill inside the loaded bar: its marker is the only thing drawn on the chart.
  vi.stubGlobal("fetch", vi.fn(async (input: Request | string) => {
    const path = new URL(input instanceof Request ? input.url : input).pathname;
    const data = path.endsWith("/config") ? { revision: 0, config: { showAiActivity: true, showTradeMarkers: true, showMarketBriefs: false, extensions: [] } }
      : path.endsWith("/activity") ? {
        environment: "simulation", replayId: null, symbol: "SHFE.au2612", capturedAt: market.capturedAt,
        dataStatus: "normal", ageSeconds: 0.3, orders: [], strategies: [], protections: [],
        fills: [{ tradeId: "t1", clientOrderId: "o1", symbol: "SHFE.au2612", direction: "BUY", offset: "OPEN", price: 824, volume: 1, tradeDateTime: market.bars[0].datetime }],
      }
      : { items: [] };
    return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
  }));
  try {
    render(<FuturesDecisionWorkspace {...props({ runtimeBaseUrl: "https://marks.example.test", preferencesScope: "scope-a" }) as any} />);
    // The 清空图上标记 entry is offered (enabled) while the fill's marker is visible…
    fireEvent.contextMenu(screen.getByLabelText("K 线图"), { clientY: 100 });
    const clear = within(screen.getByRole("menu")).getByRole("menuitem", { name: "清空图上标记" });
    await waitFor(() => expect(clear).toBeEnabled());
    fireEvent.click(clear);
    // …and retires with it: nothing is left to clear, so the entry disables.
    fireEvent.contextMenu(screen.getByLabelText("K 线图"), { clientY: 100 });
    expect(within(screen.getByRole("menu")).getByRole("menuitem", { name: "清空图上标记" })).toBeDisabled();
    // The clear is remembered per contract, not just for this render.
    expect(localStorage.getItem("ai-trader:chart-marks-cleared:scope-a:SHFE.AU2612")).toBeTruthy();
  } finally {
    vi.unstubAllGlobals();
    resetAiAnnotations();
  }
});

test("切换对话时停止等待中的语音回复：新对话里的旧回答不会被念出来", () => {
  installPttFakes();
  try {
    const view = render(<FuturesDecisionWorkspace {...props({ chatSessionId: "chat-a" }) as any} />);
    voiceReply.expect("http://runtime", []);
    expect(voiceReply.waiting).toBe(true);
    view.rerender(<FuturesDecisionWorkspace {...props({ chatSessionId: "chat-b" }) as any} />);
    expect(voiceReply.waiting).toBe(false);
  } finally {
    voiceReply.stop();
    removePttFakes();
  }
});

test("空持仓空委托时底栏自动隐藏；有持仓后照常渲染", () => {
  const values = props({ account: { ...account, positions: [] } });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  expect(screen.queryByTestId("positions-bar")).toBeNull();
  view.rerender(<FuturesDecisionWorkspace {...props() as any} />);
  expect(screen.getByTestId("positions-bar")).toHaveAttribute("data-collapsed", "true");
});

test("原生顶栏和品牌文字可以拖动，按钮与右键不会触发拖动", () => {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
  nativeWindow.startDragging.mockClear();
  try {
    const view = render(<FuturesDecisionWorkspace {...props() as any} />);
    fireEvent.mouseDown(view.container.querySelector(".fdw-brand strong")!, { button: 0 });
    expect(nativeWindow.startDragging).toHaveBeenCalledTimes(1);
    fireEvent.mouseDown(screen.getByRole("button", { name: "设置" }), { button: 0 });
    fireEvent.mouseDown(view.container.querySelector(".fdw-header")!, { button: 2 });
    expect(nativeWindow.startDragging).toHaveBeenCalledTimes(1);
  } finally {
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  }
});

test("聊天草稿跨面板和重挂载恢复，发送失败保留原文", async () => {
  const values = {...props(), workspaceScope: "owner-chat"};
  values.onPrompt.mockRejectedValue(new Error("offline"));
  const first = render(<FuturesDecisionWorkspace {...values as any}/>);
  openAiPanel();
  fireEvent.change(screen.getByLabelText("向 AI 决策流提问"), {target: {value: "还没发送的问题"}});
  fireEvent.click(screen.getByRole("button", {name: "发送给AI"}));
  await screen.findByText("发送未确认，原文已保留，请检查对话后再重试。");
  first.unmount();
  render(<FuturesDecisionWorkspace {...values as any}/>); openAiPanel();
  expect(screen.getByLabelText("向 AI 决策流提问")).toHaveValue("还没发送的问题");
});

test("一键平仓跨今昨也只提交一次、不带平今平昨：今昨由 Runtime 规划器分（意图重构第 4 步）；未确认不重提", async () => {
  const values = {...props(), account:{...account,positions:[{...account.positions[0],volume:4,todayVolume:2,historicalVolume:2}]}, onManualSubmit:vi.fn(async () => false)};
  render(<FuturesDecisionWorkspace {...values as any}/>);
  openOrderPanel();
  fireEvent.click(screen.getByRole("button", { name: "一键平仓" }));
  const dialog = screen.getByTestId("quick-order-dialog");
  expect(within(dialog).getByText("卖出平仓（今昨按设置分配）")).toBeInTheDocument();
  confirmQuickOrder();
  await screen.findByText(/本次提交未确认/);
  expect(values.onManualSubmit).toHaveBeenCalledTimes(1);
  expect(values.onManualSubmit).toHaveBeenCalledWith({side:"sell_close", volume:4, priceIntent:"aggressive", source:"three-key"});
});

test("#932 A26 一键平仓跨今昨已确认但昨仓被拒：回执按实际子单逐笔写，不说“未确认”诱导重提", async () => {
  const child = (clientOrderId: string, offset: string, volume: number, displayStatus: string) => ({ id: clientOrderId, clientOrderId, displayStatus,
    request: { symbol: "SHFE.au2612", direction: "SELL", offset, volume, priceType: "LIMIT", limitPrice: 823.98 } });
  const values = {...props(), account:{...account,positions:[{...account.positions[0],volume:4,todayVolume:2,historicalVolume:2}]},
    onManualSubmit: vi.fn(async () => ({ confirmed: true, accepted: false, skipped: [],
      orders: [child("T", "CLOSETODAY", 2, "已受理"), child("H", "CLOSE", 2, "已拒绝")] }))};
  render(<FuturesDecisionWorkspace {...values as any}/>);
  openOrderPanel();
  fireEvent.click(screen.getByRole("button", { name: "一键平仓" }));
  confirmQuickOrder();
  await screen.findByText("已确认，共 4 手：卖平今 2 手 @ 823.98 已受理、卖平昨 2 手 @ 823.98 已拒绝。系统不会自动重提，逐笔见委托分页。");
  expect(screen.queryByText(/未确认/)).toBeNull();
  expect(values.onManualSubmit).toHaveBeenCalledTimes(1);
});

test("工作区等认证连接完成后才读取设置、动态和快讯", async () => {
  resetReadIdentity();
  const paths: string[] = [];
  const fetch = vi.fn(async (request: Request | string) => {
    const path = new URL(request instanceof Request ? request.url : request).pathname;
    paths.push(path);
    const data = path.endsWith("/config")
      ? { revision: 0, config: { showAiActivity: true, showTradeMarkers: true, showMarketBriefs: true, extensions: [] } }
      : path.endsWith("/activity")
        ? { orders: [], fills: [], strategies: [], dataStatus: "normal" }
        : { items: [] };
    return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetch);
  const values = props({ runtimeBaseUrl: "https://startup.example.test", connection: "connecting" });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  try {
    await act(async () => {});
    expect(fetch).not.toHaveBeenCalled();
    view.rerender(<FuturesDecisionWorkspace {...values as any} connection="disconnected" />);
    await act(async () => {});
    expect(fetch).not.toHaveBeenCalled();
    view.rerender(<FuturesDecisionWorkspace {...values as any} connection="connected" />);
    await waitFor(() => expect(new Set(paths)).toEqual(new Set([
      "/api/app/config", "/api/app/activity", "/api/app/briefs", "/api/preferences", "/api/agent/skills", "/api/voice/realtime/status",
      "/api/market/alerts", "/api/market/drawings", "/api/reminders"
    ])));
  } finally {
    view.unmount();
    resetReadIdentity();
    vi.unstubAllGlobals();
  }
});

test("global emergency stop is visible outside AI and recovery requires explicit confirmation", async () => {
  const resume = vi.fn().mockResolvedValue(true);
  render(<FuturesDecisionWorkspace {...props({aiExecution:{controls:{stopStatus:"EMERGENCY_STOP"},authorization:null,stops:[]},onResumeAiExecution:resume}) as any}/>);
  fireEvent.click(screen.getByRole("button",{name:"AI 已急停 · 恢复"}));
  expect(screen.getByRole("dialog",{name:"恢复 AI 模拟执行"})).toBeVisible();
  expect(resume).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"暂不恢复"}));
  expect(resume).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"AI 已急停 · 恢复"}));
  fireEvent.click(screen.getByRole("button",{name:"确认恢复模拟执行"}));
  await waitFor(() => expect(resume).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.queryByRole("dialog",{name:"恢复 AI 模拟执行"})).toBeNull());
});

test("对话面板与策略面板是互斥的两个面板，一次只开一个", () => {
  const values = props({strategyPanel:<div>策略运行状态</div>});
  render(<FuturesDecisionWorkspace {...values as any}/>);
  openAiPanel();
  expect(screen.getByRole("textbox",{name:"向 AI 决策流提问"})).toBeVisible();
  expect(screen.queryByText("策略运行状态")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"打开策略面板"}));
  expect(screen.getByText("策略运行状态")).toBeVisible();
  expect(screen.queryByRole("textbox",{name:"向 AI 决策流提问"})).not.toBeInTheDocument();
  // The rail's own button closes the panel it opened.
  fireEvent.click(screen.getByRole("button",{name:"收起策略"}));
  expect(screen.getByTestId("futures-decision-workspace")).toHaveAttribute("data-right-dock", "closed");
});

test("新闻入口独立于策略且保留图表，可切研究数据", () => {
  render(<FuturesDecisionWorkspace {...props({strategyPanel:<div>策略运行状态</div>}) as any} />);
  fireEvent.click(screen.getByRole("button", {name:"打开调研面板"}));
  expect(screen.getByRole("region", {name:"调研面板"})).toBeVisible();
  expect(screen.queryByText("策略运行状态")).not.toBeInTheDocument();
  act(() => applyUiSurface("news", {tab:"data"}));
  expect(uiSurface("news")?.state).toMatchObject({tab:"data", pinnedSymbol:null});
  fireEvent.click(screen.getByRole("button", {name:"关闭调研面板"}));
  expect(screen.getByTestId("futures-decision-workspace")).toHaveAttribute("data-right-dock", "closed");
});

test("the conversation shows the batch close card of a 全部平仓 request", () => {
  const order = { symbol: "SHFE.rb2701", name: "螺纹钢2701", side: "sell_close", offset: "CLOSETODAY", positionBucket: "today", volume: 2, limitPrice: 3100, contractMultiplier: 10, riskCheck: { passed: true, limit: 0, estimatedLoss: 0, reason: "平仓", contractMultiplier: 10, priceTick: "1" } };
  const payload = { turnId: "turn-1", proposalId: "batch-1", version: 1, environment: "simulation", reason: "全部平仓", orders: [order], skipped: [], totalOrders: 1, totalVolume: 2, riskCheck: { passed: true }, updatedAt: "2026-09-18T03:00:00Z", status: "approval_required", approvalId: "approval-1" };
  const values = props({ state: { runtime: { simulationTrading: true }, turns: [{ id: "turn-1", role: "assistant", text: "", timestamp: "2026-09-18T03:00:00Z", cards: [{ id: "batch-1", type: "close-batch", payload }] }] } });
  render(<FuturesDecisionWorkspace {...values as any} />);
  openAiPanel();
  expect(screen.getByTestId("close-batch-proposal-card")).toHaveTextContent("全部平仓 1 笔 2 手");
  expect(screen.getByTestId("confirm-close-batch")).toBeEnabled();
});

test("the conversation shows a strategy's spread card with its confirm button (线上 09-29 回归：价差卡被面板白名单滤掉)", () => {
  const leg = { riskCheck: { passed: true, limit: 0, estimatedLoss: 0, reason: "", contractMultiplier: 10, priceTick: "1" }, contractMultiplier: 10, positionBucket: null, offset: "OPEN" };
  const payload = { turnId: "turn-1", proposalId: "spread-1", version: 1, environment: "simulation", reason: "价差",
    spread: { kind: "difference", legs: [] }, spreadWords: "SHFE.rb2701 − SHFE.rb2705", legsWords: "买SHFE.rb2701 1 手、卖SHFE.rb2705 1 手",
    action: "open_long", units: 1, currentSpread: -9, entrySpread: null, exitReason: null,
    orders: [{ ...leg, symbol: "SHFE.rb2701", name: "螺纹2701", side: "buy_open", volume: 1, limitPrice: 3123 },
             { ...leg, symbol: "SHFE.rb2705", name: "螺纹2705", side: "sell_open", volume: 1, limitPrice: 3132 }],
    totalOrders: 2, totalVolume: 2, riskCheck: { passed: true }, updatedAt: "2026-09-29T13:01:40Z", status: "approval_required", approvalId: "approval-1" };
  const values = props({ state: { runtime: { simulationTrading: true }, turns: [{ id: "turn-1", role: "assistant", text: "出了确认卡", timestamp: "2026-09-29T13:01:40Z", cards: [{ id: "spread-1", type: "spread", payload }] }] } });
  render(<FuturesDecisionWorkspace {...values as any} />);
  openAiPanel();
  expect(screen.getByTestId("spread-proposal-card")).toHaveTextContent("SHFE.rb2701 − SHFE.rb2705");
  expect(screen.getByTestId("confirm-spread")).toBeEnabled();
});

test("持仓带默认收起：每个持仓一枚胶囊、在途委托、持仓市值；今日净收益在顶栏账户胶囊；展开后是分页", () => {
  const values = props({ account: { ...account, account: { ...account.account, todayNetPnl: -1_250 }, positions: [{ ...account.positions[0], openPrice: 820, unrealizedPnl: 2_340 }] } });
  render(<FuturesDecisionWorkspace {...values as any} />);
  const bar = screen.getByTestId("positions-bar");
  expect(bar).toHaveAttribute("data-collapsed", "true");
  const line = screen.getByRole("button", { name: "展开持仓与委托" });
  // One chip per position (name, side and lots, floating P&L); clicking it opens the 持仓面板.
  const chip = within(bar).getByRole("button", { name: /^查看持仓 .+ 多 2 手$/ });
  expect(chip).toHaveTextContent("+2,340");
  // 820 × 1000 × 2 + 2,340 floating = last price 821.17 × 1000 × 2.
  expect(screen.getByTestId("positions-market-value")).toHaveTextContent("持仓市值 1,642,340");
  expect(within(screen.getByTestId("account-equity")).getByTestId("today-net")).toHaveTextContent("今日-1,250");
  expect(screen.getByTestId("today-net")).toHaveClass("negative");
  expect(bar).toHaveTextContent("委托 0");
  expect(bar).not.toHaveTextContent("今日成交");
  fireEvent.click(chip);
  expect(screen.getByTestId("futures-decision-workspace")).toHaveAttribute("data-right-dock", "positions");
  fireEvent.click(screen.getByRole("button", { name: "收起持仓面板" }));
  expect(screen.getByTestId("futures-decision-workspace")).toHaveAttribute("data-right-dock", "closed");
  // Expanding shows the tabs; rows carry inline labels and no header row.
  fireEvent.click(line);
  expect(screen.getByTestId("positions-bar")).toHaveAttribute("data-collapsed", "false");
  expect(within(screen.getByTestId("positions-bar")).getByRole("button", { name: /持仓 \(1\)/ })).toBeInTheDocument();
  const row = within(screen.getByTestId("positions-bar")).getByRole("list", { name: "当前持仓" }).querySelector("li")!;
  // 1.6.6: the row leads with the Chinese product name and carries 开仓均价 → 现价.
  expect(row).toHaveTextContent("沪金2612au2612买多 2 手820.00 → 824.00可用 2浮盈 +2,340平仓");
  expect(screen.getByTestId("positions-bar").querySelector("[role='table']")).toBeNull();
  // 1.6.6: the bar's own header collapses it; the rail button and Alt+P open the 持仓面板 instead.
  fireEvent.click(within(screen.getByTestId("positions-bar")).getByRole("button", { name: "收起持仓与委托" }));
  expect(screen.getByTestId("positions-bar")).toHaveAttribute("data-collapsed", "true");
  fireEvent.keyDown(window, { key: "p", altKey: true });
  expect(screen.getByTestId("futures-decision-workspace")).toHaveAttribute("data-right-dock", "positions");
  expect(screen.getByTestId("positions-bar")).toHaveAttribute("data-collapsed", "true");
  fireEvent.keyDown(window, { key: "p", altKey: true });
  expect(screen.getByTestId("futures-decision-workspace")).toHaveAttribute("data-right-dock", "closed");
});

test("持仓面板：六格 KPI（可用与风险度直报账户字段）、与底栏共用的分页与平仓，底部保护段", () => {
  const values = props({ account: { ...account, account: { ...account.account, todayNetPnl: -1_250, equity: 500_000,
    available: 380_000, margin: 120_000, riskRatio: 24 },
    positions: [{ ...account.positions[0], openPrice: 820, unrealizedPnl: 2_340, margin: 40_000 }] } });
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(screen.getByRole("button", { name: "打开持仓面板" }));
  const kpi = screen.getByTestId("positions-kpi");
  expect(kpi).toHaveTextContent("权益500,000");
  expect(kpi).toHaveTextContent("今日-1,250");
  expect(kpi).toHaveTextContent("浮动盈亏+2,340");
  expect(kpi).toHaveTextContent("可用380,000");
  expect(kpi).toHaveTextContent("保证金占用120,000");
  const riskCell = within(kpi).getByTestId("kpi-risk-ratio");
  expect(riskCell).toHaveTextContent("风险度24%");
  expect(riskCell.querySelector("dd")).toHaveAttribute("title", "保证金占用 ÷ 权益");
  expect(riskCell.querySelector("dd")).not.toHaveClass("warning");
  // The rows name the contract in Chinese and carry 开仓均价 → 现价 for the current contract.
  const firstRow = within(screen.getByLabelText("持仓面板")).getByRole("list", { name: "当前持仓" }).querySelector("li")!;
  expect(firstRow).toHaveTextContent("沪金2612");
  expect(firstRow).toHaveTextContent("820.00 → 824.00");
  const panel = screen.getByLabelText("持仓面板") as HTMLElement;
  // "查看全部" on the bar opens the same panel; the tabs share one positionsTab with it.
  fireEvent.click(within(panel).getByRole("button", { name: /委托 \(0\)/ }));
  expect(within(panel).getByRole("button", { name: "历史" })).toBeInTheDocument();
  fireEvent.click(within(panel).getByRole("button", { name: /持仓 \(1\)/ }));
  fireEvent.click(within(panel).getByRole("button", { name: "平仓 SHFE.au2612 多仓" }));
  expect(screen.getByTestId("futures-decision-workspace")).toHaveAttribute("data-right-dock", "order");
});

test("持仓面板：风险度 80% 起警告、100% 起危险；账户缺字段时显示 — 而不再折算", () => {
  const renderWithRisk = (riskRatio?: number, accountOverride: Record<string, unknown> = {}) => {
    const values = props({ account: { ...account, account: { ...account.account, ...accountOverride, riskRatio } } });
    const { unmount } = render(<FuturesDecisionWorkspace {...values as any} />);
    fireEvent.click(screen.getByRole("button", { name: "打开持仓面板" }));
    const cell = within(screen.getByTestId("positions-kpi")).getByTestId("kpi-risk-ratio").querySelector("dd")!;
    const text = cell.textContent;
    const warning = cell.className.includes("warning");
    const danger = cell.className.includes("danger");
    unmount();
    return { text, warning, danger };
  };
  expect(renderWithRisk(24)).toEqual({ text: "24%", warning: false, danger: false });
  expect(renderWithRisk(85)).toEqual({ text: "85%", warning: true, danger: false });
  expect(renderWithRisk(120)).toEqual({ text: "120%", warning: false, danger: true });
  // 旧回退（权益 × marginRatio，把百分数当小数乘出 19 亿）已删除：账户没报就是 —。
  expect(renderWithRisk(undefined, { margin: undefined, available: undefined })).toEqual({ text: "—", warning: false, danger: false });
});

test("持仓市值缺乘数时不显示，今日净收益缺字段时不显示", () => {
  const values = props({ instruments: { ...instruments, items: [] }, account: { ...account, positions: [{ ...account.positions[0], openPrice: 820 }] } });
  render(<FuturesDecisionWorkspace {...values as any} />);
  expect(screen.queryByTestId("positions-market-value")).toBeNull();
  expect(screen.queryByTestId("today-net")).toBeNull();
});

test("委托分页只列在途委托并计未知数，历史开关显示全部；顶栏不再有委托徽标与风险行", () => {
  const unknown = {
    clientOrderId: "AIT-UNKNOWN-2", proposalId: "proposal-2", displayStatus: "结果未知", submitPhase: "UNKNOWN",
    cancelPhase: "NONE", remoteStatus: "UNKNOWN",
    request: { symbol: "SHFE.au2612", direction: "BUY", offset: "OPEN", limitPrice: 824, volume: 1 }
  };
  const filled = { ...unknown, clientOrderId: "AIT-DONE-3", displayStatus: "已成交", submitPhase: "CONFIRMED", remoteStatus: "FINISHED" };
  render(<FuturesDecisionWorkspace {...props({ simulationOrders: [unknown, filled] }) as any} />);
  expect(screen.queryByTestId("orders-badge")).toBeNull();
  expect(screen.queryByTestId("risk-summary")).toBeNull();
  expect(screen.getByTestId("positions-bar")).toHaveTextContent("委托 1（未知 1）");
  openPositionsPanel();
  const bar = screen.getByTestId("positions-bar");
  fireEvent.click(within(bar).getByRole("button", { name: /委托 \(1，未知 1\)/ }));
  expect(bar.querySelector('[data-order-id="AIT-UNKNOWN-2"]')).toHaveAttribute("data-unknown", "true");
  expect(bar.querySelector('[data-order-id="AIT-DONE-3"]')).toBeNull();
  fireEvent.click(within(bar).getByRole("button", { name: "历史" }));
  expect(bar.querySelector('[data-order-id="AIT-DONE-3"]')).not.toBeNull();
  fireEvent.click(within(bar).getByRole("button", { name: "收起持仓与委托" }));
  expect(screen.getByTestId("positions-bar")).toHaveTextContent("委托 1（未知 1）");
});

test("待发送委托在委托分页给继续与放弃，放弃先确认；历史视图不给动作", () => {
  const prepared = {
    clientOrderId: "AIT-PREPARED-1", proposalId: "proposal-p", displayStatus: "待发送", submitPhase: "PREPARED",
    cancelPhase: "NONE", remoteStatus: null,
    request: { symbol: "SHFE.rb2610", direction: "BUY", offset: "OPEN", limitPrice: 3101, volume: 1 }
  };
  const values = props({ simulationOrders: [prepared] });
  render(<FuturesDecisionWorkspace {...values as any} />);
  openPositionsPanel();
  const bar = screen.getByTestId("positions-bar");
  fireEvent.click(within(bar).getByRole("button", { name: /委托 \(1\)/ }));
  const row = bar.querySelector('[data-order-id="AIT-PREPARED-1"]')!;
  fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "继续" }));
  expect(values.onOrderAction).toHaveBeenCalledWith(expect.objectContaining({ clientOrderId: "AIT-PREPARED-1" }), "continue");
  fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "放弃" }));
  expect(values.onOrderAction).toHaveBeenCalledTimes(1);
  const dialog = screen.getByTestId("abandon-order-dialog");
  expect(dialog).toHaveTextContent("SHFE.rb2610");
  fireEvent.click(within(dialog).getByRole("button", { name: "确认放弃" }));
  expect(values.onOrderAction).toHaveBeenLastCalledWith(expect.objectContaining({ clientOrderId: "AIT-PREPARED-1" }), "abandon");
  fireEvent.click(within(bar).getByRole("button", { name: "历史" }));
  expect(within(bar.querySelector('[data-order-id="AIT-PREPARED-1"]') as HTMLElement).queryByRole("button", { name: "继续" })).toBeNull();
});

const twoContracts = { ...instruments, items: [instruments.items[0], { ...instruments.items[0], symbol: "SHFE.ag2612", instrumentId: "ag2612", instrumentName: "沪银2612", productId: "ag", isMain: false }] };

test("顶栏合约标签：空时自动钉当前合约，点击切换，+ 经合约抽屉钉住，当前合约 aria-selected；复盘视图下不高亮、点标签回交易台", () => {
  const values = props({ instruments: twoContracts });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const nav = screen.getByRole("navigation", { name: "主要工作区" });
  expect(within(nav).queryByRole("button", { name: "交易台" })).toBeNull();
  expect(within(nav).getAllByRole("tab").map(tab => tab.textContent)).toEqual(["沪金2612"]);
  expect(within(nav).getByRole("tab", { name: "沪金2612" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(within(nav).getByRole("button", { name: "钉住合约" }));
  const drawer = screen.getByRole("dialog", { name: "合约列表" });
  fireEvent.click(within(drawer).getAllByRole("button", { name: /沪银 SHFE\.ag2612/ })[0]);
  expect(values.onSelectSymbol).toHaveBeenLastCalledWith("SHFE.ag2612");
  expect(within(nav).getAllByRole("tab").map(tab => tab.textContent)).toEqual(["沪金2612", "沪银2612"]);
  view.rerender(<FuturesDecisionWorkspace {...{ ...values, selectedSymbol: "SHFE.ag2612", market: { ...market, quote: { ...market.quote, symbol: "SHFE.ag2612", name: "沪银2612" } } } as any} />);
  expect(within(nav).getByRole("tab", { name: "沪银2612" })).toHaveAttribute("aria-selected", "true");
  expect(within(nav).getByRole("tab", { name: "沪金2612" })).toHaveAttribute("aria-selected", "false");
  // Review view: no contract tab is selected; clicking one returns to the trade view of that contract.
  fireEvent.click(within(nav).getByRole("button", { name: "复盘" }));
  expect(screen.getByTestId("futures-decision-workspace")).toHaveClass("fdw-review");
  expect(within(nav).getAllByRole("tab").every(tab => tab.getAttribute("aria-selected") === "false")).toBe(true);
  fireEvent.click(within(nav).getByRole("tab", { name: "沪金2612" }));
  expect(screen.getByTestId("futures-decision-workspace")).toHaveClass("fdw-trade");
  expect(values.onSelectSymbol).toHaveBeenLastCalledWith("SHFE.au2612");
  // Replay: tabs are read-only.
  view.rerender(<FuturesDecisionWorkspace {...{ ...values, replayActive: true } as any} />);
  expect(within(nav).getAllByRole("tab")).toHaveLength(2);
  expect(within(nav).queryByRole("button", { name: "钉住合约" })).toBeNull();
  expect(within(nav).queryByRole("button", { name: /移除标签/ })).toBeNull();
});

test("移除当前合约标签切到相邻标签，最后一个标签不可移除", () => {
  const values = props({ instruments: twoContracts });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const nav = screen.getByRole("navigation", { name: "主要工作区" });
  // One tab: no × at all.
  expect(within(nav).queryByRole("button", { name: /移除标签/ })).toBeNull();
  fireEvent.click(within(nav).getByRole("button", { name: "钉住合约" }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "合约列表" })).getAllByRole("button", { name: /沪银 SHFE\.ag2612/ })[0]);
  view.rerender(<FuturesDecisionWorkspace {...{ ...values, selectedSymbol: "SHFE.ag2612", market: { ...market, quote: { ...market.quote, symbol: "SHFE.ag2612", name: "沪银2612" } } } as any} />);
  values.onSelectSymbol.mockClear();
  fireEvent.click(within(nav).getByRole("button", { name: "移除标签 SHFE.ag2612" }));
  expect(values.onSelectSymbol).toHaveBeenCalledWith("SHFE.au2612");
  expect(within(nav).getAllByRole("tab").map(tab => tab.textContent)).toEqual(["沪金2612"]);
  expect(within(nav).queryByRole("button", { name: /移除标签/ })).toBeNull();
});

test("超过六个标签折进“更多合约”菜单，上限八个后不再提供钉住", () => {
  const items = ["au", "ag", "cu", "al", "zn", "rb", "ni", "sn"].map((product, index) => ({ ...instruments.items[0], symbol: `SHFE.${product}2612`, instrumentId: `${product}2612`, instrumentName: `品种${index}2612`, productId: product, isMain: false }));
  const values = props({ instruments: { ...instruments, items }, selectedSymbol: "SHFE.au2612" });
  render(<FuturesDecisionWorkspace {...values as any} />);
  const nav = screen.getByRole("navigation", { name: "主要工作区" });
  for (let index = 1; index < 8; index++) {
    fireEvent.click(within(nav).getByRole("button", { name: "钉住合约" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "合约列表" })).getAllByRole("button", { name: new RegExp(`品种 SHFE\\.${items[index].productId}2612`) })[0]);
  }
  expect(within(nav).getAllByRole("tab")).toHaveLength(6);
  expect(within(nav).queryByRole("button", { name: "钉住合约" })).toBeNull();
  fireEvent.click(within(nav).getByRole("button", { name: "更多合约 2" }));
  const more = within(nav).getByRole("menu", { name: "更多合约" });
  expect(within(more).getAllByRole("menuitem").map(item => item.textContent)).toEqual(["品种62612", "品种72612"]);
  fireEvent.click(within(more).getByRole("menuitem", { name: "品种72612" }));
  expect(values.onSelectSymbol).toHaveBeenLastCalledWith("SHFE.sn2612");
});

test("顶栏收起等级 5：合约标签收成当前合约下拉，列出钉住合约与钉住入口", () => {
  const fit = vi.spyOn(headerFit, "useHeaderFit").mockReturnValue(5);
  try {
    render(<FuturesDecisionWorkspace {...props({ instruments: twoContracts }) as any} />);
    const nav = screen.getByRole("navigation", { name: "主要工作区" });
    const trigger = within(nav).getByRole("tab", { name: "沪金2612" });
    expect(trigger).toHaveAttribute("aria-selected", "true");
    fireEvent.click(trigger);
    const menu = within(nav).getByRole("menu", { name: "钉住的合约" });
    expect(within(menu).getByRole("menuitem", { name: "沪金2612" })).toHaveAttribute("aria-current", "true");
    fireEvent.click(within(menu).getByRole("menuitem", { name: "钉住合约…" }));
    expect(screen.getByRole("dialog", { name: "合约列表" })).toBeInTheDocument();
  } finally {
    fit.mockRestore();
  }
});

test("顶栏收起等级累加：等级 6 带全部收起类名，警告与权益等仍渲染（由等级类隐藏，警告除外）", () => {
  const fit = vi.spyOn(headerFit, "useHeaderFit").mockReturnValue(6);
  try {
    render(<FuturesDecisionWorkspace {...props({ connection: "connecting" }) as any} />);
    const header = screen.getByRole("banner");
    expect(header).toHaveAttribute("data-fit", "6");
    expect(header).toHaveClass("fdw-fit-compact-ai", "fdw-fit-hide-usd", "fdw-fit-hide-text", "fdw-fit-hide-brand", "fdw-fit-contract-menu", "fdw-fit-hide-equity");
    // 必要状态永不隐藏：连接警告照常渲染，权益由等级类收起，下载入口留在侧栏。
    expect(screen.getByTestId("runtime-connection")).toHaveTextContent("正在连接");
    expect(screen.getByTestId("account-equity")).toBeInTheDocument();
    expect(header.querySelector('[data-testid="download-client"]')).toBeNull();
    expect(screen.getByRole("navigation", { name: "辅助工具" })).toContainElement(screen.getByTestId("download-client"));
    const download = screen.getByTestId("download-client");
    expect(download).toHaveTextContent("下载");
    expect(download).toHaveAttribute("href", "https://qilinshuzhi.com/trader/download");
    expect(download).toHaveAttribute("target", "_blank");
    expect(download.nextElementSibling).toBe(screen.getByRole("button", { name: /^设置$/ }));
  } finally {
    fit.mockRestore();
  }
});

test("开盘前无最新行情：K 线照画，价格位显示破折号与提示，不显示行情不可用", () => {
  const preOpen = { ...market, dataStatus: "stale" as const, isCached: true, marketSessionStatus: "closed" as const,
    quote: { ...market.quote, marketSessionStatus: "closed" as const, dataStatus: "stale" as const, dataErrorCode: "market_quote_pre_open" } };
  render(<FuturesDecisionWorkspace {...props({ market: preOpen, simulationEnabled: false, simulationBlockedReason: "行情非实时" }) as any} />);
  expect(screen.getByTestId("quote-pre-open")).toHaveTextContent("—无夜盘，开盘前无最新行情");
  expect(screen.queryByText("当前行情暂时不可用")).toBeNull();
  expect(screen.getByLabelText("K 线图")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /开盘前，查看交易时间/ })).toBeInTheDocument();
  openOrderPanel();
  expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeDisabled();
  expect(screen.getByTestId("ticket-blocked-reason")).toHaveTextContent("行情非实时");
});

test("新浪备用行情：价格旁、时段标签与连接胶囊都标明备用，下单仍不可用", () => {
  const backup = { ...market, dataStatus: "backup" as const, source: "新浪（备用）",
    quote: { ...market.quote, dataStatus: "backup" as const, source: "新浪（备用）" } };
  const runtime = { simulationTrading: true, accountStatus: "normal", marketDataStatus: "backup", marketDataSource: "新浪（备用）",
    dataConnectionStage: "reconnecting", accountMode: "tqkq-readonly", runtimeConnected: true };
  render(<FuturesDecisionWorkspace {...props({ market: backup, state: { turns: [], runtime }, simulationEnabled: false,
    simulationBlockedReason: "备用行情（新浪）只供看盘，暂不可下单" }) as any} />);
  expect(screen.getByTestId("quote-backup")).toHaveTextContent("备用 · 新浪");
  expect(screen.getByRole("button", { name: /备用行情 · 新浪，查看交易时间/ })).toHaveClass("backup");
  expect(screen.getByText("行情 · 备用（新浪，可能延迟几秒）")).toHaveAttribute("data-state", "backup");
  expect(screen.getByLabelText("K 线图")).toBeInTheDocument();
  openOrderPanel();
  expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeDisabled();
  expect(screen.getByTestId("ticket-blocked-reason")).toHaveTextContent("备用行情（新浪）只供看盘");
});

test("今日无成交：K 线照画，价格位显示最近收盘价与提示，时段标签为无成交", () => {
  const preOpen = { ...market, dataStatus: "stale" as const, isCached: true, marketSessionStatus: "closed" as const,
    quote: { ...market.quote, marketSessionStatus: "trading" as const, dataStatus: "stale" as const, dataErrorCode: "market_quote_no_trade" } };
  render(<FuturesDecisionWorkspace {...props({ market: preOpen, simulationEnabled: false, simulationBlockedReason: "行情非实时" }) as any} />);
  expect(screen.getByTestId("quote-pre-open")).toHaveTextContent("今日暂无成交，显示最近收盘价");
  expect(screen.queryByText("当前行情暂时不可用")).toBeNull();
  expect(screen.getByLabelText("K 线图")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /无成交，查看交易时间/ })).toBeInTheDocument();
  openOrderPanel();
  expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeDisabled();
  expect(screen.getByTestId("ticket-blocked-reason")).toHaveTextContent("行情非实时");
});

test("合约自身没有行情（上市以来零成交）时显示原因，重新读取降为次要，不显示行情连接中", () => {
  const runtime = { simulationTrading: true, accountStatus: "normal", marketDataStatus: "normal", dataConnectionStage: "ready", marketSessionStatus: "closed" };
  render(<FuturesDecisionWorkspace {...props({ market: undefined, marketError: "该合约上市以来没有成交，暂无行情", marketLoading: false, state: { turns: [], runtime } }) as any} />);
  expect(screen.getByText("该合约上市以来没有成交，暂无行情")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重新读取" })).toHaveClass("secondary");
  expect(screen.queryByText(/行情 · 连接中/)).toBeNull();
  expect(screen.queryByText(/已等待/)).toBeNull();
});

test("agent 界面命令 panel: positions 打开持仓面板，positionsBar 仍只管底栏；positions-drawer 是别名", () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  try {
    expect(control.mock.calls.at(-1)?.[2]).toMatchObject({ positionsBar: "collapsed", panel: null });
    act(() => { control.mock.calls.at(-1)?.[3]({ panel: "positions" } as any); });
    expect(screen.getByTestId("positions-kpi")).toBeInTheDocument();
    // The side panel does not touch the bottom bar, and the snapshot no longer reports aiPage.
    expect(control.mock.calls.at(-1)?.[2]).toMatchObject({ positionsBar: "collapsed", panel: "positions" });
    expect(control.mock.calls.at(-1)?.[2]).not.toHaveProperty("aiPage");
    act(() => { control.mock.calls.at(-1)?.[3]({ positionsBar: "expanded" } as any); });
    expect(screen.getByTestId("positions-bar")).toHaveAttribute("data-collapsed", "false");
    expect(control.mock.calls.at(-1)?.[2]).toMatchObject({ positionsBar: "expanded", panel: "positions" });
    act(() => { control.mock.calls.at(-1)?.[3]({ panel: null } as any); });
    expect(screen.queryByTestId("positions-kpi")).toBeNull();
    // 1.6.6 alias: the old 持仓抽屉 dialog name opens and closes the same panel.
    act(() => { control.mock.calls.at(-1)?.[3]({ dialog: { name: "positions-drawer", open: true } } as any); });
    expect(screen.getByTestId("positions-kpi")).toBeInTheDocument();
    expect(control.mock.calls.at(-1)?.[2]).toMatchObject({ panel: "positions", dialog: null });
    act(() => { control.mock.calls.at(-1)?.[3]({ dialog: { name: "positions-drawer", open: false } } as any); });
    expect(screen.queryByTestId("positions-kpi")).toBeNull();
  } finally {
    view.unmount(); control.mockRestore();
  }
});

test("退役的 aiFilter / aiPage 只打开对话面板；strategyTab 打开策略面板并切页签，快照报 strategyTab", () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const panel = vi.fn((_: Record<string, unknown>) => <div>策略运行状态</div>);
  function Panel(values: Record<string, unknown>) { return panel(values); }
  const view = render(<FuturesDecisionWorkspace {...props({ strategyPanel: <Panel /> }) as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  try {
    expect(state()).toMatchObject({ panel: null, strategyTab: "library" });
    expect(state()).not.toHaveProperty("aiFilter");
    act(() => { control.mock.calls.at(-1)?.[3]({ aiFilter: "trading" } as any); });
    expect(state()).toMatchObject({ panel: "ai" });
    expect(screen.queryByRole("button", { name: "策略与交易" })).toBeNull();
    act(() => { control.mock.calls.at(-1)?.[3]({ panel: null } as any); });
    act(() => { control.mock.calls.at(-1)?.[3]({ aiPage: "monitor" } as any); });
    expect(state()).toMatchObject({ panel: "ai" });
    act(() => { control.mock.calls.at(-1)?.[3]({ strategyTab: "running" } as any); });
    expect(state()).toMatchObject({ panel: "strategy", strategyTab: "running" });
    expect(panel.mock.calls.at(-1)?.[0]).toMatchObject({ tab: "running" });
    // The panel's own tab change is the same state.
    act(() => { (panel.mock.calls.at(-1)?.[0].onTabChange as (tab: string) => void)("detail"); });
    expect(state()).toMatchObject({ strategyTab: "detail" });
  } finally {
    view.unmount(); control.mockRestore();
  }
});

test("图表工具栏没有“策略信号”按钮；对象的 AI 菜单以用户身份发问、带上 focus，并打开对话", async () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const panel = vi.fn((_: Record<string, unknown>) => <div>策略运行状态</div>);
  function Panel(values: Record<string, unknown>) { return panel(values); }
  const values = props({ strategyPanel: <Panel /> });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  try {
    expect(screen.queryByRole("button", { name: /策略信号/ })).toBeNull();
    act(() => { control.mock.calls.at(-1)?.[3]({ strategyTab: "running" } as any); });
    expect(panel.mock.calls.at(-1)?.[0]).toMatchObject({ explainBlocked: null, sessionId: (values as { chatSessionId?: string }).chatSessionId });
    const focus = { kind: "strategy_instance", id: "run-1", label: "双均线 · 螺纹2701" };
    act(() => { (panel.mock.calls.at(-1)?.[0].onAskAi as (text: string, focus: unknown) => void)("这个策略为什么没开仓", focus); });
    await waitFor(() => expect(values.onPrompt).toHaveBeenCalledWith("这个策略为什么没开仓", expect.objectContaining({ focus })));
    expect(control.mock.calls.at(-1)?.[2]).toMatchObject({ panel: "ai" });
  } finally {
    view.unmount(); control.mockRestore();
  }
});

test("positionsTab / ordersHistory 在持仓面板开着时不顶开底栏，面板没开时照旧展开", () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  try {
    // Panel open: the user is watching it, so the bar under the chart stays as it was.
    act(() => { control.mock.calls.at(-1)?.[3]({ panel: "positions" } as any); });
    act(() => { control.mock.calls.at(-1)?.[3]({ positionsTab: "orders" } as any); });
    expect(control.mock.calls.at(-1)?.[2]).toMatchObject({ panel: "positions", positionsTab: "orders", positionsBar: "collapsed" });
    act(() => { control.mock.calls.at(-1)?.[3]({ ordersHistory: true } as any); });
    expect(control.mock.calls.at(-1)?.[2]).toMatchObject({ ordersHistory: true, positionsBar: "collapsed" });
    // Panel closed: the records only exist in the bar, so it is expanded as before.
    act(() => { control.mock.calls.at(-1)?.[3]({ panel: null } as any); });
    // `trades` (平仓记录) is the old name of 成交; the state reports the new one.
    act(() => { control.mock.calls.at(-1)?.[3]({ positionsTab: "trades" } as any); });
    expect(control.mock.calls.at(-1)?.[2]).toMatchObject({ positionsTab: "fills", positionsBar: "expanded" });
    // fillsSource opens the 持仓面板's 成交 tab with that 来源 chip pressed.
    act(() => { control.mock.calls.at(-1)?.[3]({ fillsSource: "stop" } as any); });
    expect(control.mock.calls.at(-1)?.[2]).toMatchObject({ panel: "positions", positionsTab: "fills", fillsSource: "stop" });
    expect(screen.getByRole("button", { name: "止损" })).toHaveAttribute("aria-pressed", "true");
  } finally {
    view.unmount(); control.mockRestore();
  }
});

test("Alt+A 开关对话面板，Alt+P 开关持仓面板", () => {
  render(<FuturesDecisionWorkspace {...props() as any} />);
  const shell = screen.getByTestId("futures-decision-workspace");
  fireEvent.keyDown(window, { key: "a", altKey: true });
  expect(shell).toHaveAttribute("data-right-dock", "ai");
  fireEvent.keyDown(window, { key: "a", altKey: true });
  expect(shell).toHaveAttribute("data-right-dock", "closed");
  fireEvent.keyDown(window, { key: "p", altKey: true });
  expect(shell).toHaveAttribute("data-right-dock", "positions");
});

test("委托价可直接编辑并按一跳步进，提交的是编辑后的价格", async () => {
  const values = props();
  render(<FuturesDecisionWorkspace {...values as any} />);
  openOrderPanel();
  const price = await screen.findByLabelText("模拟委托价") as HTMLInputElement;
  expect(price.value).toBe("824.00");  // seeded from the last price without a chart pick
  fireEvent.click(screen.getByRole("button", { name: "委托价加一跳" }));
  expect(price.value).toBe("824.02");
  fireEvent.change(price, { target: { value: "823.51" } });
  fireEvent.blur(price);
  expect(price.value).toBe("823.52");  // snapped onto the 0.02 tick grid on blur
  fireEvent.click(screen.getByRole("button", { name: "买多" }));
  fireEvent.click(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ }));
  await waitFor(() => expect(values.onManualSubmit).toHaveBeenCalledTimes(1));
  expect(values.onManualSubmit).toHaveBeenCalledWith(expect.objectContaining({ side: "buy_open", limitPrice: 823.52, priceIntent: "passive" }));
  // A chart pick re-seeds the field (the ladder replaces the order panel, so reopen it).
  selectCurrentPrice();
  openOrderPanel();
  expect((screen.getByLabelText("模拟委托价") as HTMLInputElement).value).toBe("824.00");
  fireEvent.change(screen.getByLabelText("模拟委托价"), { target: { value: "" } });
  expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeDisabled();
  // The reason sits right under the fields, on the estimate line (#789).
  expect(screen.getByTestId("ticket-estimate")).toHaveTextContent("请输入委托价");
});

test("不可下单时按钮下方显示具体的未满足条件", async () => {
  render(<FuturesDecisionWorkspace {...props({ simulationEnabled: false, simulationBlockedReason: "行情已 23 秒未更新" }) as any} />);
  openOrderPanel();
  expect(await screen.findByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeDisabled();
  expect(screen.getByTestId("ticket-blocked-reason")).toHaveTextContent("行情已 23 秒未更新");
});

test("合约抽屉按品种两级：品种行选主力，“N 个月份”展开，精确合约代码自动展开并高亮", () => {
  const catalog = { ...instruments, items: [instruments.items[0], { ...instruments.items[0], symbol: "SHFE.ag2612", instrumentId: "ag2612", instrumentName: "沪银2612", productId: "ag", isMain: false }] };
  const values = props({ instruments: catalog, selectedSymbol: "SHFE.ag2612" });
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(screen.getByRole("button", { name: /^(沪金2612|沪银2612)/ }));
  const drawer = screen.getByRole("dialog", { name: "合约列表" });
  // 最近 stays; below it the products grouped under their exchange, one row per 品种 — the face
  // is the main contract (沪金) or the nearest expiry for a product the exchange has not marked (沪银).
  expect(within(drawer).getAllByRole("heading", { level: 4 }).map(node => node.textContent)).toEqual(["最近", "上期所"]);
  const products = within(drawer).getAllByRole("button", { name: /^沪[金银] SHFE\./ });
  expect(products.map(node => node.getAttribute("aria-label"))).toEqual(["沪金 SHFE.au2612", "沪银 SHFE.ag2612"]);
  expect(products[1]).toHaveClass("active");
  // Collapsed by default: no month rows. Expanding 沪银 shows its contract by expiry.
  expect(within(drawer).queryAllByRole("button", { name: /^SHFE\.ag2612$/ })).toHaveLength(0);
  fireEvent.click(within(drawer).getAllByRole("button", { name: "1 个月份" })[1]);
  fireEvent.click(within(drawer).getByRole("button", { name: "SHFE.ag2612" }));
  expect(values.onSelectSymbol).toHaveBeenLastCalledWith("SHFE.ag2612");
  // Selecting closes the drawer; reopen it for the search behaviour.
  fireEvent.click(screen.getByRole("button", { name: /^(沪金2612|沪银2612)/ }));
  const reopened = screen.getByRole("dialog", { name: "合约列表" });
  // A 品种名 search keeps the matching product with all its months; a contract code
  // auto-expands its product and highlights the month.
  fireEvent.change(within(reopened).getByPlaceholderText("搜索合约代码或名称"), { target: { value: "沪银" } });
  expect(within(reopened).getAllByRole("button", { name: /^沪[金银] SHFE\./ })).toHaveLength(1);
  fireEvent.change(within(reopened).getByPlaceholderText("搜索合约代码或名称"), { target: { value: "ag2612" } });
  expect(within(reopened).getByRole("button", { name: "SHFE.ag2612" })).toHaveClass("hit");
  fireEvent.change(within(reopened).getByPlaceholderText("搜索合约代码或名称"), { target: { value: "zn" } });
  expect(within(reopened).getByText("没有匹配的品种或合约")).toBeInTheDocument();
});

test("恢复对话框列出急停时暂停的策略并可跳到策略页", () => {
  const values = props({ aiExecution: { authorization: { status: "EMERGENCY_STOP" }, controls: { stopStatus: "EMERGENCY_STOP", generation: 1, revision: 1, maxOrderVolume: null, maxOrders: null }, stops: [] },
    pausedStrategyIds: ["strategy-a", "strategy-b"], onResumeAiExecution: vi.fn(async () => true), strategyPanel: <div>策略面板</div> });
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(screen.getByRole("button", { name: /AI 已急停 · 恢复/ }));
  const list = screen.getByRole("list", { name: "急停时暂停的策略" });
  expect(within(list).getAllByRole("listitem")).toHaveLength(2);
  fireEvent.click(within(list).getAllByRole("button", { name: "去启动" })[0]);
  expect(screen.getByTestId("futures-decision-workspace")).toHaveAttribute("data-right-dock", "strategy");
});

test("观察快照给区域稳定的 name", () => {
  render(<FuturesDecisionWorkspace {...props() as any} />);
  const names = Array.from(document.querySelectorAll("[data-region]")).map(node => node.getAttribute("data-region"));
  expect(names).toEqual(expect.arrayContaining(["chart.main"]));
});

test("agent 界面命令（第十一轮）：底栏分页、历史开关、对话框与抽屉、下单区页、选持仓、免确认、钉住合约都可命令并回报", async () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const values = props({ preferencesScope: "scope-a" });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  const send = (changes: Record<string, unknown>) => act(() => { control.mock.calls.at(-1)?.[3](changes as any); });
  // Native dialog state reaches the registry through a MutationObserver (a microtask).
  const settle = () => act(async () => { await Promise.resolve(); });
  try {
    // One au position: it is the implicit selection for that contract from the start.
    expect(state()).toMatchObject({ positionsTab: "positions", quickOrderConfirm: true, dialog: null, selectedPosition: { symbol: "SHFE.au2612", direction: "long" }, pinnedSymbols: ["SHFE.au2612"] });
    send({ positionsTab: "fills" });
    expect(state()).toMatchObject({ positionsBar: "expanded", positionsTab: "fills" });
    send({ ordersHistory: true });
    expect(state()).toMatchObject({ positionsTab: "orders", ordersHistory: true });
    expect(screen.getByRole("button", { name: "历史" })).toHaveAttribute("aria-pressed", "true");
    send({ dialog: { name: "contract-drawer", open: true } });
    expect(screen.getByRole("dialog", { name: "合约列表" })).toBeInTheDocument();
    send({ dialog: { name: "contract-drawer", open: false } });
    send({ dialog: { name: "emergency-stop", open: true } });
    expect(screen.getByRole("button", { name: "确认全局急停" })).toBeInTheDocument();
    expect(state()).toMatchObject({ dialog: "emergency-stop" });
    send({ dialog: { name: "emergency-stop", open: false } });
    expect(state()).toMatchObject({ dialog: null });
    // Selecting a position opens the ticket's close page for it; null clears the selection.
    send({ selectPosition: { symbol: "SHFE.au2612", direction: "long" } });
    expect(state()).toMatchObject({ panel: "order", view: "trade", ticketMode: "close", selectedPosition: { symbol: "SHFE.au2612", direction: "long" } });
    expect(() => send({ selectPosition: { symbol: "SHFE.au2612", direction: "short" } })).toThrow("没有这个持仓");
    send({ ticketMode: "open" });
    expect(state()).toMatchObject({ ticketMode: "open" });
    // The third ticket page is a command too; its snapshot shows the page and the difference preview.
    send({ ticketMode: "target" });
    expect(state()).toMatchObject({ ticketMode: "target" });
    expect(screen.getByTestId("target-plan-summary")).toHaveTextContent("当前 +2 手");
    send({ ticketMode: "open" });
    send({ selectPosition: null });
    // #932 P2: null clears it even for a lone position — re-choosing it on every render left the command
    // never applied and the window answering 409 to everything after it.
    expect(state()).toMatchObject({ selectedPosition: null });
    send({ quickOrderConfirm: false });
    expect(state()).toMatchObject({ quickOrderConfirm: false });
    expect(localStorage.getItem("ai-trader:quick-order-skip-confirm:scope-a")).toBe("1");
    expect(() => send({ pinnedSymbols: { remove: "SHFE.au2612" } })).toThrow("最后一个合约标签不能移除");
    expect(() => send({ pinnedSymbols: { add: "DCE.i2609" } })).toThrow("合约不存在");
    send({ dialog: { name: "help", open: true, topic: "strategy" } });
    await settle();
    expect(state()).toMatchObject({ dialog: "help" });
    expect(screen.getByRole("dialog", { name: "使用说明" })).toHaveAttribute("open");
    send({ dialog: { name: "help", open: false } });
    await settle();
    expect(state()).toMatchObject({ dialog: null });
  } finally {
    view.unmount(); control.mockRestore(); localStorage.removeItem("ai-trader:quick-order-skip-confirm:scope-a");
  }
});

test("agent 界面命令：chartView 切分时并把数据周期钉到 60，任何 duration 命令都回 K 线视图（快照同时上报）", async () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const onDurationChange = vi.fn();
  const view = render(<FuturesDecisionWorkspace {...props({ onDurationChange }) as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  const send = (changes: Record<string, unknown>) => act(() => { control.mock.calls.at(-1)?.[3](changes as any); });
  try {
    expect(state()).toMatchObject({ chartView: "kline", duration: 300 });
    const group = screen.getByRole("group", { name: "K线周期" });
    expect(within(group).getByRole("button", { name: "5分" })).toHaveClass("active");
    send({ chartView: "timeshare" });
    expect(state()).toMatchObject({ chartView: "timeshare" });   // 分时是 60 秒 serial 的视图，不是周期
    expect(onDurationChange).toHaveBeenCalledWith(60);
    expect(within(group).getByRole("button", { name: "分时" })).toHaveClass("active");
    expect(within(group).getByRole("button", { name: "5分" })).not.toHaveClass("active");
    send({ duration: 1800 });
    expect(state()).toMatchObject({ chartView: "kline" });       // duration 命令与点周期按钮同一条路
    expect(onDurationChange).toHaveBeenLastCalledWith(1800);
    expect(within(group).getByRole("button", { name: "分时" })).not.toHaveClass("active");
  } finally {
    view.unmount(); control.mockRestore();
  }
});

test("agent 界面命令：chat-history 折叠列出历史对话，chatSession 切换到列表中的会话或新建会话并回报 chatSessionId", async () => {
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const onChangeChat = vi.fn(async () => undefined);
  const onListChats = vi.fn(async () => ({ items: [{ sessionId: "s-old", createdAt: "2026-09-18T01:00:00Z" }, { sessionId: "s-now", createdAt: "2026-09-18T02:00:00Z" }] }));
  const values = props({ chatSessionId: "s-now", onChangeChat, onListChats });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  const send = (changes: Record<string, unknown>) => act(() => { control.mock.calls.at(-1)?.[3](changes as any); });
  const settle = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });
  try {
    send({ aiPage: "chat" });
    expect(state()).toMatchObject({ chatSessionId: "s-now", chatHistory: null });
    expect((state().disclosures as Record<string, boolean>)["chat-history"]).toBe(false);
    // An unknown id is refused until the list has been read.
    expect(() => send({ chatSession: { sessionId: "s-old" } })).toThrow("历史对话");
    act(() => { uiSurface("disclosure:chat-history")!.apply!(true); });
    await settle();
    expect(onListChats).toHaveBeenCalledOnce();
    expect(state()).toMatchObject({ chatHistory: [{ sessionId: "s-old" }, { sessionId: "s-now" }] });
    expect((state().disclosures as Record<string, boolean>)["chat-history"]).toBe(true);
    expect(screen.getByRole("button", { name: /2026.*当前/ })).toBeDisabled();
    send({ chatSession: { sessionId: "s-old" } });
    await settle();
    expect(onChangeChat).toHaveBeenLastCalledWith("s-old");
    // Switching closes the list, like a click does.
    expect(state()).toMatchObject({ chatHistory: null });
    send({ chatSession: { new: true } });
    await settle();
    expect(onChangeChat).toHaveBeenLastCalledWith(undefined);
  } finally {
    view.unmount(); control.mockRestore();
  }
});


/** 设置重做第 2 步: a Runtime that answers /api/preferences; every other read gets an empty body. */
function preferencesRuntime(initial: Record<string, unknown> = {}) {
  let document = { version: 1, preferences: { ...DEFAULT_PREFERENCES, ...initial } };
  const puts: { url: string; body: Record<string, unknown> }[] = [];
  const fetch = vi.fn(async (input: Request | string, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    if (path === "/api/preferences") {
      if (request.method === "PUT") {
        const body = JSON.parse(await request.text());
        puts.push({ url: request.url, body });
        const merge = (base: Record<string, unknown>, patch: Record<string, unknown>): Record<string, unknown> => Object.fromEntries([...Object.keys(base), ...Object.keys(patch)].map(key =>
          [key, Array.isArray(patch[key]) ? patch[key]
            : patch[key] !== undefined && typeof patch[key] === "object" && patch[key] !== null ? merge((base[key] ?? {}) as Record<string, unknown>, patch[key] as Record<string, unknown>) : patch[key] !== undefined ? patch[key] : base[key]]));
        document = { version: document.version + 1, preferences: merge(document.preferences, body) as typeof document.preferences };
      }
      return new Response(JSON.stringify(document), { headers: { "Content-Type": "application/json" } });
    }
    const data = path.endsWith("/config") ? { revision: 0, config: { showAiActivity: true, showTradeMarkers: true, showMarketBriefs: false, extensions: [] } }
      : path.endsWith("/activity") ? { orders: [], fills: [], strategies: [], dataStatus: "normal" }
      : path.endsWith("/trading-costs") ? { source: "account", default: { mode: "per_lot", open: 10, closeToday: 20, closeYesterday: 30 }, overrides: [] } : { items: [] };
    return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetch);
  return { puts, current: () => document };
}

test("设置第 2 步：各类页的偏好行写 PUT /api/preferences，并作用于图表、下单台与发送键", async () => {
  const runtime = preferencesRuntime({ chart: { ...DEFAULT_PREFERENCES.chart, defaultDuration: 900, subPanes: { kd: false, macd: true } }, trade: { ...DEFAULT_PREFERENCES.trade, defaultVolume: 3 } });
  const values = props({ runtimeBaseUrl: "https://runtime.example.test", duration: 300 });
  render(<FuturesDecisionWorkspace {...values as any} />);
  // Account defaults land once the document is read: chart period, 副图 panes and the ticket volume.
  await waitFor(() => expect(values.onDurationChange).toHaveBeenCalledWith(900));
  fireEvent.click(screen.getByRole("button", { name: "打开模拟下单" }));
  expect(screen.getByLabelText("模拟委托手数")).toHaveValue(3);

  fireEvent.click(screen.getByTitle("设置"));
  const settings = screen.getByRole("dialog", { name: "设置" });
  fireEvent.click(within(settings).getByRole("button", { name: "图表" }));
  fireEvent.click(within(settings).getByRole("radio", { name: "绿涨红跌" }));
  await waitFor(() => expect(document.documentElement.dataset.updown).toBe("intl"));
  // 指标参数在图表页的二级页（指标参数与均线）里；副图格子也只在这一页编辑（一级页的
  // KD/MACD 开关 2026-10-03 与图上编辑重复，已删）。kd:false、macd:true → 只剩 MACD 一格。
  fireEvent.click(within(settings).getByRole("button", { name: "打开指标参数与均线" }));
  expect(within(settings).getByText("第 1 格 · MACD")).toBeVisible();
  expect(within(settings).queryAllByText(/^第 \d+ 格 · /)).toHaveLength(1);
  fireEvent.click(within(settings).getByRole("button", { name: "MACD 快线增加" }));
  await waitFor(() => expect(runtime.current().preferences.chart.macdParams.fast).toBe(13));
  fireEvent.click(within(settings).getByRole("button", { name: "交易" }));
  // 平今平昨、语音下单免口头确认在交易页的二级页（高级交易设置）里。
  fireEvent.click(within(settings).getByRole("button", { name: "打开高级交易设置" }));
  fireEvent.click(within(settings).getByRole("switch", { name: "语音下单免口头确认" }));
  await waitFor(() => expect(runtime.current().preferences.trade.voiceOrderConfirm).toBe(false));
  expect(within(settings).queryByRole("switch", { name: "闪电下单" })).not.toBeInTheDocument();
  fireEvent.click(within(settings).getByRole("button", { name: "AI 与对话" }));
  fireEvent.click(within(settings).getByRole("radio", { name: "Ctrl / Cmd + Enter" }));
  await waitFor(() => expect(runtime.current().preferences.chat.sendKey).toBe("ctrlEnter"));
  fireEvent.click(within(settings).getByRole("button", { name: "数据与隐私" }));
  fireEvent.click(within(settings).getByRole("button", { name: "会话保留天数减少" }));
  await waitFor(() => expect(runtime.current().preferences.data.sessionRetentionDays).toBe(358));
  // 设置 has no 提醒 page since 2026-10-25: its switches are the 提醒 panel's 通用提醒 tab.
  expect(within(settings).queryByRole("button", { name: "提醒" })).toBeNull();
  fireEvent.click(within(settings).getByRole("button", { name: "关闭系统设置" }));
  fireEvent.click(screen.getByRole("button", { name: "打开提醒面板" }));
  fireEvent.click(screen.getByRole("tab", { name: "通用提醒" }));
  const general = screen.getByRole("tabpanel", { name: "通用提醒" });
  // 通用提醒 (#932, 2026-10-15): one switch per kind over every channel; 急停 always reminds (a locked
  // line, no switch) and 成交与撤单 writes both keys.
  expect(within(general).queryByRole("switch", { name: /急停/ })).toBeNull();
  expect(within(general).getByRole("region", { name: "总是提醒" })).toHaveTextContent("总是提醒，不能关");
  expect(within(general).getAllByRole("region").map(section => section.getAttribute("aria-label")).slice(0, 3))
    .toEqual(["交易与账户 · 进对话", "信息 · 只进提醒记录", "总是提醒"]);
  expect(within(general).getAllByRole("region").at(-1)).toHaveAttribute("aria-label", "锁屏推送");
  expect(within(general).getByRole("combobox", { name: "保本提醒门槛" })).toHaveValue("multiple:1");
  // 外盘（美元）(2026-10-21): only in the amount mode; a number writes amountUsd, empty writes null (外盘按倍数).
  expect(within(general).queryByRole("spinbutton", { name: "外盘（美元）" })).toBeNull();
  fireEvent.change(within(general).getByRole("combobox", { name: "保本提醒门槛" }), { target: { value: "amount:2000" } });
  await waitFor(() => expect(runtime.current().preferences.notify.breakEvenRule).toMatchObject({ mode: "amount", amount: 2000, amountUsd: null }));
  fireEvent.change(within(general).getByRole("spinbutton", { name: "外盘（美元）" }), { target: { value: "300" } });
  await waitFor(() => expect(runtime.current().preferences.notify.breakEvenRule).toMatchObject({ mode: "amount", amount: 2000, amountUsd: 300 }));
  fireEvent.change(within(general).getByRole("spinbutton", { name: "外盘（美元）" }), { target: { value: "" } });
  await waitFor(() => expect(runtime.current().preferences.notify.breakEvenRule?.amountUsd).toBeNull());
  fireEvent.click(within(general).getByRole("switch", { name: "成交与撤单" }));
  await waitFor(() => expect(runtime.current().preferences.notify.desktop).toMatchObject({ fills: false, cancels: false }));
  fireEvent.click(within(general).getByRole("switch", { name: "收盘前持仓" }));
  await waitFor(() => expect(runtime.current().preferences.notify.marketCloseReminder).toBe(true));
  fireEvent.click(within(general).getByRole("switch", { name: "风险度" }));
  await waitFor(() => expect(runtime.current().preferences.notify.riskReminder).toBe(false));
  fireEvent.click(within(general).getByRole("switch", { name: "盘中异动" }));
  await waitFor(() => expect(runtime.current().preferences.notify.movement).toBe(false));
  // AI 简报订阅 (2026-10-05): the 订阅品种 link on the AI 简讯 row opens 生成简讯 and the products block.
  expect(within(general).queryByRole("region", { name: "订阅品种" })).toBeNull();
  fireEvent.click(within(general).getByRole("button", { name: /^AI 简讯的订阅品种/ }));
  expect(within(general).getByRole("button", { name: /^AI 简讯的订阅品种/ })).toHaveAttribute("aria-expanded", "true");
  expect(within(general).getByRole("switch", { name: "生成简讯" })).toBeVisible();
  expect(within(general).getByRole("region", { name: "订阅品种" })).toBeVisible();
  // Every write carried the version it was based on (the lock), and each key became a partial document.
  expect(runtime.puts.every(put => /\?version=\d+$/.test(put.url))).toBe(true);
  expect(runtime.puts[0].body).toEqual({ chart: { upDownColors: "intl" } });
  fireEvent.click(screen.getByRole("button", { name: "关闭提醒面板" }));

  // Ctrl/Cmd+Enter: Enter alone no longer submits the composer.
  fireEvent.click(screen.getByRole("button", { name: "打开对话" }));
  const composer = screen.getByLabelText("向 AI 决策流提问");
  fireEvent.change(composer, { target: { value: "现在怎么看" } });
  fireEvent.keyDown(composer, { key: "Enter" });
  expect(values.onPrompt).not.toHaveBeenCalled();
  fireEvent.keyDown(composer, { key: "Enter", ctrlKey: true });
  await waitFor(() => expect(values.onPrompt).toHaveBeenCalled());

  // A depth price fills the ticket without opening a draft.
  openOrderPanel();
  fireEvent.click(within(screen.getByRole("region", { name: "五档" })).getByRole("button", { name: /^卖一/ }));
  expect(screen.queryByTestId("ticket-draft")).not.toBeInTheDocument();
  expect(screen.getByLabelText("模拟委托价")).toHaveValue(market.quote.askPrice1);
  expect(values.onManualSubmit).not.toHaveBeenCalled();
  document.documentElement.removeAttribute("data-updown");
});

test("报价总表：列、排序与交易所筛选从账号偏好恢复，改动各写回 marketBoard", async () => {
  const runtime = preferencesRuntime({ marketBoard: {
    columns: ["last", "volume", "preSettlement", "quoteTime"],
    sort: { key: "volume", order: "asc" }, exchange: "SHFE" } });
  const values = props({ runtimeBaseUrl: "https://runtime.example.test", duration: 300 });
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(screen.getByRole("button", { name: "报价" }));
  // The document decides: only the configured columns show, 成交量 sorts ascending, 上期所 is pressed.
  await waitFor(() => expect(screen.getByRole("button", { name: "成交量排序" })).toHaveClass("active"));
  const board = screen.getByTestId("market-board");
  expect(within(board).getByText("昨结")).toBeVisible();
  expect(within(board).getByText("更新时间")).toBeVisible();
  expect(within(board).queryByText("涨跌幅")).toBeNull();
  expect(within(board).getByRole("button", { name: "成交量排序" })).toHaveTextContent("▲");
  expect(within(board).getByRole("button", { name: "上期所" })).toHaveAttribute("aria-pressed", "true");
  // 列编辑保存整张列序（勾选即在末尾加一列）；浮层本身是可命令的 disclosure。
  fireEvent.click(within(board).getByRole("button", { name: "列" }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "列设置" })).getByRole("checkbox", { name: "最高" }));
  await waitFor(() => expect(runtime.current().preferences.marketBoard?.columns)
    .toEqual(["last", "volume", "preSettlement", "quoteTime", "high"]));
  fireEvent.click(within(screen.getByRole("dialog", { name: "列设置" })).getByRole("button", { name: "关闭" }));
  expect(screen.queryByRole("dialog", { name: "列设置" })).toBeNull();
  expect(uiSurface("disclosure:board-columns")?.state).toBe(false);
  act(() => { uiSurface("disclosure:board-columns")!.apply!(true); });
  expect(screen.getByRole("dialog", { name: "列设置" })).toBeVisible();
  expect(uiSurface("disclosure:board-columns")?.state).toBe(true);
  act(() => { uiSurface("disclosure:board-columns")!.apply!(false); });
  expect(screen.queryByRole("dialog", { name: "列设置" })).toBeNull();
  // 排序与筛选的点击 500 ms 后写回（每次等待都过了一个防抖窗口，各自成为一次 partial PUT）。
  fireEvent.click(within(board).getByRole("button", { name: "成交量排序" }));
  await waitFor(() => expect(runtime.puts.some(put =>
    JSON.stringify(put.body) === JSON.stringify({ marketBoard: { sort: { key: "volume", order: "desc" } } }))).toBe(true), { timeout: 3000 });
  fireEvent.click(within(board).getByRole("button", { name: "全部" }));
  await waitFor(() => expect(runtime.puts.some(put =>
    JSON.stringify(put.body) === JSON.stringify({ marketBoard: { exchange: "all" } }))).toBe(true), { timeout: 3000 });
  expect(runtime.current().preferences.marketBoard?.sort).toEqual({ key: "volume", order: "desc" });
  expect(runtime.current().preferences.marketBoard?.exchange).toBe("all");
});

test("#522 报价总表交易所筛选写回失败后回到账号文档的值，不一直停在本地值", async () => {
  preferencesRuntime({ marketBoard: { columns: ["last", "volume"], sort: { key: "volume", order: "asc" }, exchange: "SHFE" } });
  const documentFetch = globalThis.fetch;
  const puts: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: Request | string, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    if (request.method === "PUT" && new URL(request.url).pathname === "/api/preferences") {
      puts.push(request.url);
      return new Response(JSON.stringify({ detail: "invalid" }), { status: 422, headers: { "Content-Type": "application/json" } });
    }
    return documentFetch(request);
  }));
  render(<FuturesDecisionWorkspace {...props({ runtimeBaseUrl: "https://runtime.example.test", duration: 300 }) as any} />);
  fireEvent.click(screen.getByRole("button", { name: "报价" }));
  const board = screen.getByTestId("market-board");
  await waitFor(() => expect(within(board).getByRole("button", { name: "上期所" })).toHaveAttribute("aria-pressed", "true"));
  fireEvent.click(within(board).getByRole("button", { name: "全部" }));
  expect(within(board).getByRole("button", { name: "全部" })).toHaveAttribute("aria-pressed", "true");
  await waitFor(() => expect(puts).toHaveLength(1), { timeout: 3000 });
  await waitFor(() => expect(within(board).getByRole("button", { name: "上期所" })).toHaveAttribute("aria-pressed", "true"));
});

/** 自选连点: like preferencesRuntime, but the first PUT stays in flight until released and the
 *  board itself has rows, so the stars of the 报价 view are clickable. */
function watchlistRuntime() {
  let version = 1;
  let document: { version: number; preferences: Record<string, any> } =
    { version, preferences: { ...DEFAULT_PREFERENCES } };
  const puts: { url: string; body: Record<string, unknown> }[] = [];
  let releaseFirstPut: (() => void) | undefined;
  const board = {
    schemaVersion: 1, capturedAt: "2026-10-03T02:32:05Z", dataStatus: "normal", revision: "e:1", truncated: false,
    rows: [
      { symbol: "SHFE.rb2610", instrumentId: "rb2610", instrumentName: "螺纹钢2610", productName: "螺纹钢",
        exchangeId: "SHFE", productId: "rb", last: 3118, preSettlement: 3087, change: 31, changePercent: 1,
        volume: 1_523_000, openInterest: 2_017_000, openInterestChange: -34_000, high: 3130, low: 3080,
        upperLimit: 3334, lowerLimit: 2840, quoteTime: "2026-10-03 10:32:05.000000", status: "normal" },
      { symbol: "SHFE.ag2612", instrumentId: "ag2612", instrumentName: "白银2612", productName: "白银",
        exchangeId: "SHFE", productId: "ag", last: 8400, preSettlement: 8214, change: 186, changePercent: 2.26,
        volume: 981_000, openInterest: 512_000, openInterestChange: 1_200, high: 8420, low: 8201,
        upperLimit: 8800, lowerLimit: 7800, quoteTime: "2026-10-03 10:32:05.000000", status: "normal" },
    ],
  };
  const fetch = vi.fn(async (input: Request | string, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    if (path === "/api/preferences") {
      if (request.method === "PUT") {
        const body = JSON.parse(await request.text());
        puts.push({ url: request.url, body });
        version += 1;
        if (puts.length === 1) await new Promise<void>(resolve => { releaseFirstPut = resolve; });
        document = { version, preferences: { ...document.preferences, ...body } };
      }
      return new Response(JSON.stringify(document), { headers: { "Content-Type": "application/json" } });
    }
    if (path === "/api/market/board") {
      return new Response(JSON.stringify(board), { headers: { "Content-Type": "application/json" } });
    }
    const data = path.endsWith("/config") ? { revision: 0, config: { showAiActivity: true, showTradeMarkers: true, showMarketBriefs: false, extensions: [] } }
      : path.endsWith("/activity") ? { orders: [], fills: [], strategies: [], dataStatus: "normal" }
      : path.endsWith("/trading-costs") ? { source: "account", default: { mode: "per_lot", open: 10, closeToday: 20, closeYesterday: 30 }, overrides: [] } : { items: [] };
    return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetch);
  return { puts, current: () => document, release: () => { releaseFirstPut?.(); } };
}

test("自选连点：第一个保存没回来，第二个星标在最新文档上叠加", async () => {
  const runtime = watchlistRuntime();
  const values = props({ runtimeBaseUrl: "https://runtime.example.test", duration: 300 });
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(screen.getByRole("button", { name: "报价" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "加入自选 螺纹钢" })).toBeVisible());
  // 两下连点之间第一个 PUT 还挂着：修复前列表按渲染时的旧值整发，第二下会丢掉第一下。
  fireEvent.click(screen.getByRole("button", { name: "加入自选 螺纹钢" }));
  await waitFor(() => expect(runtime.puts).toHaveLength(1));  // 第一个 PUT 已发出、还没回来
  fireEvent.click(screen.getByRole("button", { name: "加入自选 白银" }));
  expect(runtime.puts).toHaveLength(1);  // 第二下还排在队列里
  act(() => { runtime.release(); });
  await waitFor(() => expect(runtime.puts).toHaveLength(2));
  expect(runtime.puts.map(put => put.body)).toEqual([
    { watchlist: { products: ["SHFE.rb"] } },
    { watchlist: { products: ["SHFE.rb", "SHFE.ag"] } },
  ]);
  expect(runtime.current().preferences.watchlist?.products).toEqual(["SHFE.rb", "SHFE.ag"]);
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "移出自选 螺纹钢" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "移出自选 白银" })).toHaveAttribute("aria-pressed", "true");
  });
  vi.unstubAllGlobals();
});

test("快捷键页可录制、拒绝冲突并恢复默认；录制的组合键立即生效", () => {
  const values = props();
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(screen.getByTitle("设置"));
  const settings = screen.getByRole("dialog", { name: "设置" });
  fireEvent.click(within(settings).getByRole("button", { name: "快捷键" }));
  fireEvent.click(within(settings).getByRole("button", { name: "录制打开 / 收起 持仓面板" }));
  fireEvent.keyDown(window, { key: "a", altKey: true });
  expect(within(settings).getByText("Alt+A 已用于「打开 / 收起 对话」")).toBeVisible();
  fireEvent.click(within(settings).getByRole("button", { name: "录制打开 / 收起 持仓面板" }));
  fireEvent.keyDown(window, { key: "o", altKey: true, shiftKey: true });
  expect(within(settings).getByText("已改为 Alt+Shift+O")).toBeVisible();
  expect(JSON.parse(localStorage.getItem("ai-trader:shortcuts") ?? "{}").positions).toBe("Alt+Shift+O");
  fireEvent.click(within(settings).getByRole("button", { name: "恢复默认快捷键" }));
  expect(within(settings).getByRole("button", { name: "恢复默认快捷键" })).toBeDisabled();
});

/** M2 用户档位: a cloud Runtime that answers the plan surfaces the workspace itself
 *  consumes (owner status, model catalog with quota, preferences, skill registry).
 *  `registry` adds GET /api/agent/skills; without it that endpoint 404s, so the client
 *  falls back to the legacy `features` view. The admin console's own endpoints moved to
 *  AdminApp.test.tsx with the console (the /trader/admin split, 2026-09-30). */
function plansRuntime(options: { owner: boolean; registry?: boolean; accounts?: Record<string, unknown>;
  suggest?: (body: Record<string, unknown>) => Response }) {
  const ownerStatus = options.owner
    ? { authenticated: true, authMode: "owner-password", authProvider: "owner-password", username: "boss", userId: "owner-boss", role: "owner", plan: "owner", planLabel: "管理员",
        features: { aiExecution: true, webResearch: true, strategyTools: true, marketBriefs: true, strategies: true, backtests: true, replays: true, voice: true } }
    : { authenticated: true, authMode: "owner-password", authProvider: "owner-password", username: "u1", userId: "owner-u1", role: "user", plan: "basic", planLabel: "基础档",
        features: { aiExecution: false, webResearch: false, strategyTools: false, marketBriefs: true, strategies: true, backtests: false, replays: false, voice: true } };
  const models = options.owner
    ? { current: "glm-5.3-flash", models: [
        { slug: "deepseek-flash", displayName: "DeepSeek Flash", provider: "deepseek", tier: "all", configured: true, selectable: true, memberOnly: false, inPlan: true },
        { slug: "glm-5.3-flash", displayName: "GLM 5.3 Flash", provider: "zhipu", tier: "all", configured: true, selectable: true, memberOnly: false, inPlan: true }],
      quota: { dailyTokenLimit: null, usedTokensToday: 12_345, day: "2026-09-22", plan: "owner", planLabel: "管理员", voiceSecondsToday: 90, voiceSecondsPerDay: null } }
    : { current: "deepseek-flash", models: [
        { slug: "deepseek-flash", displayName: "DeepSeek Flash", provider: "deepseek", tier: "all", configured: true, selectable: true, memberOnly: false, inPlan: true },
        { slug: "glm-5.3-flash", displayName: "GLM 5.3 Flash", provider: "zhipu", tier: "all", configured: true, selectable: false, memberOnly: false, inPlan: false }],
      quota: { dailyTokenLimit: 300_000, usedTokensToday: 1_200, day: "2026-09-22", plan: "basic", planLabel: "基础档", voiceSecondsToday: 300, voiceSecondsPerDay: 1_800 } };
  // The registry as the current user sees it: availability follows the viewer (the owner sees
  // everything), not the owner's 技能 matrix (that harness lives in AdminApp.test.tsx now).
  const agentSkills = [
    { id: "admin-console", label: "后台管理", category: "系统", description: "成员与档位管理控制台", tierGated: true, ownerOnly: true,
      available: options.owner, unavailableReason: options.owner ? null : "owner_only", phrases: [], uiEntries: [], quickAction: null },
    { id: "help", label: "使用说明", category: "系统", description: "内置手册与操作指引", tierGated: false, ownerOnly: false,
      available: true, unavailableReason: null, phrases: [], uiEntries: [], quickAction: "prompt:帮助" },
    { id: "ai-execution", label: "AI 自主执行", category: "交易", description: "AI 直接执行模拟下单与止损", tierGated: true, ownerOnly: false,
      available: options.owner, unavailableReason: options.owner ? null : "not_in_plan", phrases: [], uiEntries: [], quickAction: null },
    { id: "market-replay", label: "行情回放", category: "复盘", description: "历史行情回放工作台", tierGated: true, ownerOnly: false,
      available: false, unavailableReason: "disabled", phrases: [], uiEntries: [], quickAction: null },
    { id: "strategy-signals", label: "策略信号解读", category: "策略", description: "解读当前策略的信号并标注", tierGated: true, ownerOnly: false,
      available: options.owner, unavailableReason: options.owner ? null : "owner_only", phrases: [], uiEntries: [], quickAction: "prompt:按我的策略现在该怎么做" },
    { id: "strategy-backtests", label: "回测与体检", category: "策略", description: "回测选中的策略并体检", tierGated: true, ownerOnly: false,
      available: true, unavailableReason: null, phrases: [], uiEntries: [], quickAction: "prompt:回测我选中的策略" }];
  let prefsDoc = { version: 1, preferences: DEFAULT_PREFERENCES };
  const calls: { method: string; path: string; body?: Record<string, unknown> }[] = [];
  const json = (data: unknown) => new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
  const fetch = vi.fn(async (input: Request | string, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    const body = request.body == null ? undefined : JSON.parse(await request.text());
    calls.push({ method: request.method, path, body });
    if (path === "/api/auth/owner/status") return json(ownerStatus);
    if (path === "/api/models") return json(models);
    if (path === "/api/agent/skills") {
      if (!options.registry) return new Response("not found", { status: 404 });  // a pre-registry Runtime
      return json({ items: agentSkills });
    }
    if (path === "/api/accounts" && options.accounts) return json(options.accounts);
    if (path === "/api/agent/suggestions" && options.suggest) return options.suggest(body as Record<string, unknown>);
    if (path === "/api/preferences" && request.method === "PUT") {
      prefsDoc = { version: prefsDoc.version + 1, preferences: { ...prefsDoc.preferences, ...(body as object) } };
      return json(prefsDoc);
    }
    if (path === "/api/preferences") return json(prefsDoc);
    const data = path.endsWith("/config") ? { revision: 0, config: { showAiActivity: true, showTradeMarkers: true, showMarketBriefs: false, extensions: [] } }
      : path.endsWith("/activity") ? { orders: [], fills: [], strategies: [], dataStatus: "normal" }
      : path.endsWith("/trading-costs") ? { source: "account", default: { mode: "per_lot", open: 10, closeToday: 20, closeYesterday: 30 }, overrides: [] }
      : path.endsWith("/realtime/status") ? { enabled: true, keyConfigured: true, reason: null }
      : { items: [] };
    return json(data);
  });
  vi.stubGlobal("fetch", fetch);
  return { calls };
}

test("M2 基础档：设置显示档位与用量，模型下拉只列档位内模型，未开通功能点击说明且不执行", async () => {
  const runtime = plansRuntime({ owner: false });
  const values = props({ mode: "cloud", runtimeBaseUrl: "https://basic.example.test" });
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(screen.getByTitle("设置"));
  const settings = screen.getByRole("dialog", { name: "设置" });
  await waitFor(() => expect(within(settings).getByText("档位").closest(".app-setting-row")).toHaveTextContent("基础档"));
  // 2026-10-08: the quotas fold under the tier name.
  fireEvent.click(within(settings).getByRole("button", { name: "基础档：展开每日额度" }));
  expect(within(settings).getByText("今日 token").closest(".app-setting-row")).toHaveTextContent("1,200 / 300,000");
  expect(within(settings).getByText("今日语音").closest(".app-setting-row")).toHaveTextContent("5 分钟 / 30 分钟");
  // AI 模型行随设置拆页 (2026-09-30) 移到「AI 与对话」。
  fireEvent.click(within(settings).getByRole("button", { name: "AI 与对话" }));
  const modelSelect = within(settings).getByRole("combobox", { name: "切换模型" });
  expect(within(modelSelect).getAllByRole("option").map(option => option.textContent)).toEqual(["DeepSeek Flash"]);
  fireEvent.click(within(settings).getByRole("button", { name: "交易" }));
  const autonomy = within(settings).getByRole("switch", { name: "AI 自主执行" });
  expect(autonomy).toBeEnabled();
  fireEvent.click(autonomy);
  expect(runtime.calls.some(call => call.path === "/api/preferences" && call.method === "PUT")).toBe(false);
  expect(within(settings).getByText("当前档位未开通「AI 自主执行」")).toBeVisible();
  // 未开通入口保留点击反馈，不能执行受限功能。
  expect(screen.getByRole("button", { name: "回放" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "回放" }));
  expect(values.onOpenReplay).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "回放" })).toHaveAttribute("title", "当前档位未开通「行情回放」");
  expect(screen.getByRole("button", { name: "打开策略面板" })).toBeEnabled();
  fireEvent.click(within(settings).getByRole("button", { name: "关闭系统设置" }));
});

test("后台管理已拆到独立网页：连主账号的顶栏也没有后台按钮，设置里没有用户管理页", async () => {
  plansRuntime({ owner: true });
  render(<FuturesDecisionWorkspace {...props({ mode: "cloud", runtimeBaseUrl: "https://owner.example.test" }) as any} />);
  fireEvent.click(screen.getByTitle("设置"));
  const settings = screen.getByRole("dialog", { name: "设置" });
  // The quota row filling proves the owner status has landed — the console moved to
  // /trader/admin (2026-09-30), so neither surface may render an entry any more.
  await waitFor(() => expect(within(settings).getByText("档位").closest(".app-setting-row")).toHaveTextContent("管理员"));
  expect(screen.queryByRole("button", { name: "后台管理" })).toBeNull();
  expect(within(settings).queryByRole("button", { name: "用户管理" })).toBeNull();
  expect(screen.queryByRole("region", { name: "后台管理" })).toBeNull();
});

test("技能注册表视图：拒绝原因与服务端同文案，快捷技能点击可用才发送、设置钉选写偏好", async () => {
  const runtime = plansRuntime({ owner: false, registry: true });
  const values = props({ mode: "cloud", runtimeBaseUrl: "https://basic.example.test" });
  render(<FuturesDecisionWorkspace {...values as any} />);
  // 顶栏回放：注册表说全局停用（对所有人生效）→ title 给统一原因行。
  const replay = await screen.findByRole("button", { name: "回放" });
  expect(replay).toBeEnabled();
  fireEvent.click(replay);
  expect(values.onOpenReplay).not.toHaveBeenCalled();
  expect(screen.getByText("「行情回放」已停用")).toBeVisible();
  expect(replay).toHaveAttribute("title", "「行情回放」已停用");
  // 设置 → 交易：AI 自主执行档位未开 → 与服务端 unavailable_line 同文案。
  fireEvent.click(screen.getByTitle("设置"));
  const settings = screen.getByRole("dialog", { name: "设置" });
  await waitFor(() => expect(within(settings).getByText("档位").closest(".app-setting-row")).toHaveTextContent("基础档"));
  fireEvent.click(within(settings).getByRole("button", { name: "交易" }));
  const autonomy = within(settings).getByRole("switch", { name: "AI 自主执行" });
  expect(autonomy).toBeEnabled();
  fireEvent.click(autonomy);
  expect(runtime.calls.some(call => call.path === "/api/preferences" && call.method === "PUT")).toBe(false);
  expect(within(settings).getByText("当前档位未开通「AI 自主执行」")).toBeVisible();
  // 设置 → AI 与对话：快捷技能钉选来自注册表（有 quickAction 的技能各一行），默认一个不钉，勾选写 ui.quickSkills。
  fireEvent.click(within(settings).getByRole("button", { name: "AI 与对话" }));
  const lastPut = () => runtime.calls.filter(call => call.path === "/api/preferences" && call.method === "PUT").at(-1)?.body;
  fireEvent.click(within(settings).getByRole("switch", { name: "使用说明" }));
  await waitFor(() => expect(lastPut()).toEqual({ ui: { quickSkills: ["help"] } }));
  fireEvent.click(within(settings).getByRole("switch", { name: "回测与体检" }));
  await waitFor(() => expect(lastPut()).toEqual({ ui: { quickSkills: ["help", "strategy-backtests"] } }));
  fireEvent.click(within(settings).getByRole("switch", { name: "策略信号解读" }));
  await waitFor(() => expect(lastPut()).toEqual({ ui: { quickSkills: ["help", "strategy-backtests", "strategy-signals"] } }));
  fireEvent.click(within(settings).getByRole("button", { name: "关闭系统设置" }));
  // 对话面板快捷栏：可用时发送 quickAction 文本，不可用时点击解释原因且不发送。
  openAiPanel();
  const bar = screen.getByLabelText("快捷技能");
  const guided = within(bar).getByRole("button", { name: "回测与体检" });
  expect(guided).toBeEnabled();
  expect(guided).toHaveAttribute("title", "发送：回测我选中的策略");
  const signals = within(bar).getByRole("button", { name: "策略信号解读" });
  expect(signals).toBeEnabled();
  expect(signals).toHaveAttribute("title", "「策略信号解读」仅主账号可用");
  fireEvent.click(signals);
  expect(values.onPrompt).not.toHaveBeenCalled();
  fireEvent.click(guided);
  await waitFor(() => expect(values.onPrompt).toHaveBeenCalledWith("回测我选中的策略", expect.anything()));
});

test("按住说话的快捷键全局生效：对话面板没开时按下右 Alt 先打开面板；被阻塞时不录音，只在输入行给原因", () => {
  const getUserMedia = vi.fn();
  Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
  render(<FuturesDecisionWorkspace {...props() as any} />);
  expect(screen.queryByLabelText("向 AI 决策流提问")).not.toBeInTheDocument();
  const down = new KeyboardEvent("keydown", { code: "AltRight", key: "Alt", bubbles: true, cancelable: true });
  act(() => { window.dispatchEvent(down); });
  expect(down.defaultPrevented).toBe(true);
  // No Runtime URL in this fixture: blocked — the panel opens with the reason, nothing records.
  expect(screen.getByLabelText("向 AI 决策流提问")).toBeInTheDocument();
  expect(screen.getByText("连接后可用", { selector: ".fdw-ptt-notice" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "按住说话" })).toBeDisabled();
  expect(getUserMedia).not.toHaveBeenCalled();
  act(() => { window.dispatchEvent(new KeyboardEvent("keyup", { code: "AltRight", key: "Alt", bubbles: true })); });
  Object.defineProperty(navigator, "mediaDevices", { value: undefined, configurable: true });
});


test("游客登录保留15分钟当前周期，账号旧5分钟默认值不覆盖且不改合约", async () => {
  preferencesRuntime({ chart: { ...DEFAULT_PREFERENCES.chart, defaultDuration: 300, upDownColors: "intl" } });
  const values = props({ runtimeBaseUrl: "https://runtime.example.test", duration: 900, selectedSymbol: "SHFE.rb2610" });
  const consume = vi.fn();
  render(<GuestAppearanceContext.Provider value={{theme:"light",duration:900,consume}}><FuturesDecisionWorkspace {...values as any} /></GuestAppearanceContext.Provider>);
  // Only the first member workspace takes the guest's view; a remount (回放) uses the account's.
  expect(consume).toHaveBeenCalledTimes(1);
  // This marker proves the actual account preference response has been applied, including its old 5-minute default.
  await waitFor(() => expect(document.documentElement.dataset.updown).toBe("intl"));
  expect(values.onDurationChange).not.toHaveBeenCalled();
  expect(values.onSelectSymbol).not.toHaveBeenCalled();
  expect(document.querySelector('[aria-label="K线周期"] .active')).toHaveTextContent("15分");
});

it("a price alert crossing always shows the in-app notice, even without desktop permission — once, from feed.reminder", async () => {
  // 10-02 本机演示：桌面通知被拒、对话栏收起时，到价提醒触发后界面上什么都不出现。统一提醒 (#932, 2026-10-15):
  // the crossing's notification and notice come from its feed.reminder only; feed.price-alert re-reads the alerts.
  const desktopNotification = vi.fn();
  vi.stubGlobal("Notification", Object.assign(desktopNotification, { permission: "granted" }));
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  try {
    act(() => {
      applyFeedEvent("s", { schemaVersion: 1, id: "pa", sessionId: "s", kind: "result", name: "feed.price-alert",
        timestamp: "now", payload: { id: "a1", symbol: "SHFE.au2612", name: "沪金2612", direction: "above",
          price: "8000", lastPrice: 8001, note: "", text: "到价提醒：沪金2612 涨到 8000（现 8001）。" } } as any, 1000);
    });
    expect(desktopNotification).not.toHaveBeenCalled();
    act(() => {
      applyFeedEvent("s", { schemaVersion: 1, id: "rm", sessionId: "s", kind: "result", name: "feed.reminder",
        timestamp: "now", payload: { id: "r1", type: "new", kind: "price_alert", title: "到价提醒 · 沪金2612",
          text: "到价提醒：沪金2612 涨到 8000（现 8001）。" } } as any, 1000);
    });
    expect(desktopNotification).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("strategy-signal-notice")).toHaveTextContent("到价提醒：沪金2612 涨到 8000");
    // A reminder whose state changed (type update) re-reads the 提醒中心 without a second notification.
    act(() => {
      applyFeedEvent("s", { schemaVersion: 1, id: "ru", sessionId: "s", kind: "result", name: "feed.reminder",
        timestamp: "now", payload: { id: "r1", type: "update", kind: "price_alert", title: "到价提醒 · 沪金2612", text: "x" } } as any, 1000);
    });
    expect(desktopNotification).toHaveBeenCalledTimes(1);
  } finally {
    view.unmount();
    vi.unstubAllGlobals();
  }
});

it("a feed.reminder shows the desktop notification (when permitted) and the in-app notice", async () => {
  // 10-03 收盘前/风险度提醒：Runtime 已按用户开关把关，客户端只负责提示——桌面通知按
  // 权限弹（标题来自事件），界内提示条与到价提醒同一条，被拒也不静默。
  const desktopNotification = vi.fn();
  vi.stubGlobal("Notification", Object.assign(desktopNotification, { permission: "granted" }));
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  try {
    act(() => {
      applyFeedEvent("s", { schemaVersion: 1, id: "rm", sessionId: "s", kind: "result", name: "feed.reminder",
        timestamp: "now", payload: { kind: "market_close_day", title: "收盘提醒",
          text: "收盘提醒：4 分钟后日盘收盘，你还有 螺纹钢2610 多 2 手（浮盈 +3,000）未平。" } } as any, 1000);
    });
    expect(desktopNotification).toHaveBeenCalledWith("收盘提醒",
      expect.objectContaining({ body: expect.stringContaining("螺纹钢2610") }));
    expect(await screen.findByTestId("strategy-signal-notice")).toHaveTextContent("收盘提醒：4 分钟后日盘收盘");
  } finally {
    view.unmount();
    vi.unstubAllGlobals();
  }
});

/** 手机版: the window answers the phone-layout query (a narrow touch screen) or not. */
function stubPhone(phone: boolean) {
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
    matches: phone && query === MOBILE_LAYOUT_QUERY, media: query, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  })));
}

const phoneTurns = [
  { id: "t1", role: "assistant", text: "", timestamp: "2026-10-04T01:30:00Z", completed: true, cards: [{ id: "j1", type: "research-judgment", payload: {
    turnId: "t1", cardId: "j1", symbol: "SHFE.au2612", productName: "沪金", status: "ready", stance: "偏多", confidence: "中", headline: "库存回落，站上均线",
    facts: [{ text: "社会库存周降 4.2%" }], invalidation: [], generatedAt: "2026-10-04T01:30:00Z", researchDay: "2026-10-04",
    actions: [{ kind: "ui", label: "打开调研", changes: { news: { tab: "desk" } } }] } }] },
  { id: "t2", role: "assistant", text: "", timestamp: "2026-10-04T02:42:00Z", completed: true, cards: [{ id: "r1", type: "reminder", payload: {
    turnId: "t2", cardId: "r1", kind: "price_alert", title: "到价提醒", summary: "沪金2612 到了 824", figures: [{ label: "触发价", value: "824" }],
    symbol: "SHFE.au2612", at: "2026-10-04T02:42:00Z",
    actions: [{ kind: "ask", label: "问 AI 怎么办", prompt: "沪金到价了怎么办" }, { kind: "ui", label: "打开持仓", changes: { panel: "positions" } }] } }] },
  { id: "t3", role: "assistant", text: "", timestamp: "2026-10-04T01:58:00Z", completed: true, cards: [{ id: "e1", type: "execution", payload: {
    turnId: "t3", cardId: "e1", source: "ai", sourceLabel: "AI 自主", orderKind: "order", symbol: "SHFE.au2612", name: "沪金2612", sideLabel: "买多",
    volume: 1, price: "823", status: "filled", statusLabel: "已成交", updatedAt: "2026-10-04T01:58:00Z" } }] },
];

test("手机版：窄屏触屏进入手机布局（底部五个标签、按住说话、没有右侧工具栏）；电脑与回放保持电脑布局（游客见 GuestEntry.test）", () => {
  stubPhone(false);
  const desktop = render(<FuturesDecisionWorkspace {...props() as any} />);
  expect(screen.getByTestId("futures-decision-workspace")).toBeInTheDocument();
  expect(screen.queryByTestId("mobile-workspace")).toBeNull();
  expect(screen.queryByRole("navigation", { name: "主导航" })).toBeNull();
  desktop.unmount();

  stubPhone(true);
  const replay = render(<FuturesDecisionWorkspace {...props({ replayActive: true }) as any} />);
  expect(screen.getByTestId("futures-decision-workspace")).toBeInTheDocument();
  replay.unmount();

  render(<FuturesDecisionWorkspace {...props() as any} />);
  expect(screen.getByTestId("mobile-workspace")).toBeInTheDocument();
  expect(screen.queryByTestId("futures-decision-workspace")).toBeNull();
  expect(screen.queryByLabelText("K 线图")).toBeNull();
  const nav = screen.getByRole("navigation", { name: "主导航" });
  expect(within(nav).getAllByRole("button").map(button => button.textContent)).toEqual(["AI", "调研", "看盘", "策略", "我的"]);
  expect(within(nav).getByRole("button", { name: "AI" })).toHaveAttribute("aria-current", "page");
  expect(screen.getByText("AI", { selector: ".fdm-head > strong" })).toBeVisible();
  expect(screen.getByRole("button", { name: /^看盘：沪金2612 824/ })).toBeVisible();
  expect(screen.getByRole("button", { name: "看提醒" })).toBeVisible();
  expect(screen.getByRole("button", { name: "打字问 AI" })).toBeVisible();
  expect(screen.getByRole("button", { name: "按住说话，松开发送" })).toBeVisible();
});

test("手机版标签与界面命令 mobileTab：对话即 panel: ai，侧边面板与视图命令被拒绝，快照上报 mobileLayout / mobileTab", () => {
  stubPhone(true);
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  const apply = (changes: Record<string, unknown>) => act(() => { control.mock.calls.at(-1)?.[3](changes as any); });
  const nav = () => screen.getByRole("navigation", { name: "主导航" });
  try {
    expect(state()).toMatchObject({ mobileLayout: true, mobileTab: "ai", panel: null });
    fireEvent.click(within(nav()).getByRole("button", { name: "调研" }));
    expect(screen.getByTestId("mobile-research")).toHaveTextContent("调研");
    expect(state()).toMatchObject({ mobileTab: "research" });
    apply({ mobileTab: "chat" });
    expect(screen.getByRole("region", { name: "AI 对话" })).toBeVisible();
    expect(screen.getByRole("button", { name: "按住说话，松开发送" })).toBeVisible();
    expect(within(nav()).getByRole("button", { name: "AI" })).toHaveAttribute("aria-current", "page");
    expect(state()).toMatchObject({ mobileTab: "chat", panel: "ai" });
    fireEvent.click(screen.getByRole("button", { name: "返回 AI 首页" }));
    expect(state()).toMatchObject({ mobileTab: "ai", panel: null });
    apply({ panel: "ai" });
    expect(state()).toMatchObject({ mobileTab: "chat" });
    expect(() => apply({ panel: "order" })).toThrow("手机版没有这个界面");
    expect(() => apply({ view: "board" })).toThrow("手机版没有这个界面");
    expect(() => apply({ positionsBar: "expanded" })).toThrow("手机版没有这个界面");
    apply({ mobileTab: "me" });
    expect(screen.getByTestId("mobile-me")).toHaveTextContent("我的");
    expect(state()).toMatchObject({ mobileTab: "me", panel: null });
    // 新结构 (2026-10-06): 策略 is a tab; the retired 通知 opens AI 首页 (its filter kept) and reports ai.
    apply({ mobileTab: "strategy" });
    expect(screen.getByTestId("mobile-strategy")).toHaveTextContent("正在运行");
    expect(state()).toMatchObject({ mobileTab: "strategy", mobileStrategyTab: "running" });
    // 第八批 (2026-10-08): the 策略 tab's own two tabs are a command and a snapshot field.
    apply({ mobileStrategyTab: "mine" });
    expect(state()).toMatchObject({ mobileTab: "strategy", mobileStrategyTab: "mine" });
    apply({ mobileStrategyTab: "running" });
    apply({ mobileNotifyFilter: "fill" });
    apply({ mobileTab: "me" });
    apply({ mobileTab: "notify" });
    expect(screen.getByTestId("mobile-feed")).toBeVisible();
    expect(state()).toMatchObject({ mobileTab: "ai", mobileNotifyFilter: "fill" });
    // 回测结果页: one comparison by command; the snapshot names it while the page shows.
    apply({ mobileLab: { labId: "lab-1" } });
    expect(screen.getByTestId("mobile-lab")).toBeVisible();
    expect(state()).toMatchObject({ mobilePage: "lab", mobileLab: { labId: "lab-1", chosen: null } });
    apply({ mobilePage: null });
    expect(state()).not.toHaveProperty("mobileLab");
  } finally {
    view.unmount(); control.mockRestore();
  }
  // The desktop layout has no tabs: the command is refused and the heartbeat carries no phone field.
  stubPhone(false);
  const desktopControl = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const desktop = render(<FuturesDecisionWorkspace {...props() as any} />);
  try {
    expect(desktopControl.mock.calls.at(-1)?.[2]).not.toHaveProperty("mobileTab");
    expect(desktopControl.mock.calls.at(-1)?.[2]).not.toHaveProperty("mobileLayout");
    expect(() => act(() => { desktopControl.mock.calls.at(-1)?.[3]({ mobileTab: "ai" } as any); })).toThrow("电脑布局");
  } finally {
    desktop.unmount(); desktopControl.mockRestore();
  }
});

test("手机版 AI 首页：今天概况三格、时间线行按时间倒序（成交 / 判断；提醒在提醒中心），点一行去对话或持仓；整张判断卡不再出现在首页", async () => {
  stubPhone(true);
  const values = props({ state: { turns: phoneTurns, runtime: { simulationTrading: true } } });
  render(<FuturesDecisionWorkspace {...values as any} />);
  const home = screen.getByTestId("mobile-feed");
  const tiles = within(home).getByRole("group", { name: "今天概况" });
  expect(within(tiles).getByRole("button", { name: /^持仓/ })).toHaveTextContent("1 笔浮盈 +2,340");
  expect(within(tiles).getByRole("button", { name: /^待确认/ })).toHaveTextContent("待确认0");
  expect(within(home).queryByTestId("research-judgment-card")).toBeNull();
  const rows = () => within(screen.getByRole("list", { name: "时间线" })).getAllByRole("button");
  expect(rows().map(row => row.getAttribute("aria-label"))).toEqual([
    "成交 · 买多 沪金2612 1 手 @ 823", "AI · 今日判断已更新：沪金偏多"]);
  // 持仓 → 持仓与委托 (full screen); its close returns to AI 首页.
  fireEvent.click(within(tiles).getByRole("button", { name: /^持仓/ }));
  expect(screen.getByTestId("mobile-positions")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "收起持仓" }));
  fireEvent.click(rows()[1]);
  expect(screen.getByRole("region", { name: "AI 对话" })).toBeVisible();
});

test("手机版 AI 首页：待确认的卡用对话里同一张卡置顶，确认走现有确认流程；处理后回到时间线原位，它正在盯着随之出现", () => {
  stubPhone(true);
  const issued = new Date().toISOString();
  const tradeTurn = (status: string) => ({ id: "tp", role: "assistant", text: "", timestamp: issued, completed: true, cards: [{ id: "p1", type: "trade", payload: {
    turnId: "tp", proposalId: "p1", version: 1, symbol: "SHFE.au2612", name: "沪金2612", side: "sell_close", offset: "CLOSE", volume: 2, limitPrice: 823,
    contractMultiplier: 1000, updatedAt: issued, environment: "simulation", status, approvalId: "ap1", executionEligibleAtCreation: true,
    riskCheck: { passed: true, limit: 0, estimatedLoss: 0, reason: "", contractMultiplier: 1000, priceTick: "0.02" } } }] });
  const values = props({ state: { turns: [...phoneTurns.slice(0, 1), tradeTurn("approval_required")], runtime: { simulationTrading: true } } });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const home = () => screen.getByTestId("mobile-feed");
  const pin = () => home().querySelector(".fdm-pin");
  expect(pin()).not.toBeNull();
  expect(within(pin() as HTMLElement).getByTestId("trade-proposal-card")).toHaveTextContent("卖出平仓");
  expect(within(home()).getByRole("button", { name: /^待确认/ })).toHaveTextContent("1 张还剩 10 分钟");
  expect(within(home()).queryByTestId("mobile-watch")).toBeNull();
  expect(within(home()).queryByRole("button", { name: /卖出平仓/ })).toBeNull();
  fireEvent.click(within(pin() as HTMLElement).getByTestId("confirm-trade"));
  expect(values.proposalActions.confirm).toHaveBeenCalledWith(expect.objectContaining({ id: "p1", type: "trade" }));
  // Confirmed: off the pin, back in the timeline at its own time; nothing waits, so 它正在盯着 shows.
  view.rerender(<FuturesDecisionWorkspace {...props({ state: { turns: [...phoneTurns.slice(0, 1), tradeTurn("candidate_recorded")], runtime: { simulationTrading: true } } }) as any} />);
  expect(pin()).toBeNull();
  const rows = within(screen.getByRole("list", { name: "时间线" })).getAllByRole("button").map(row => row.getAttribute("aria-label"));
  expect(rows[0]).toBe("已确认 · 卖出平仓 沪金2612 2 手 @ 823");
  expect(within(home()).getByTestId("mobile-watch")).toHaveTextContent("1 笔持仓沪金多单 2 手");
});

test("手机版 AI 首页渲染后会停下来：确认卡的时钟只按定时器走，不随时间线数据的每次重算再触发渲染（10-06 更新循环回归）", async () => {
  stubPhone(true);
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const issued = new Date().toISOString();
  const values = props({ state: { turns: [...phoneTurns, { id: "tp", role: "assistant", text: "", timestamp: issued, completed: true, cards: [{ id: "p1", type: "trade", payload: {
    turnId: "tp", proposalId: "p1", version: 1, symbol: "SHFE.au2612", name: "沪金2612", side: "buy_open", offset: "OPEN", volume: 1, limitPrice: 823,
    contractMultiplier: 1000, updatedAt: issued, environment: "simulation", status: "approval_required", approvalId: "ap1",
    riskCheck: { passed: true, limit: 0, estimatedLoss: 0, reason: "", contractMultiplier: 1000, priceTick: "0.02" } } }] }], runtime: { simulationTrading: true } } });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  try {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 50)); });
    const settled = control.mock.calls.length;
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 200)); });
    expect(control.mock.calls.length - settled).toBeLessThan(5);
  } finally { view.unmount(); control.mockRestore(); }
});

/** 手机版第二期: the Runtime reads a phone window makes, by path (anything else answers an empty list). */
function phoneFetch(routes: Record<string, unknown>) {
  vi.stubGlobal("fetch", vi.fn(async (input: Request | string) => {
    const url = new URL(input instanceof Request ? input.url : input);
    const path = url.pathname;
    const hit = Object.entries({ "/api/preferences": { version: 1, preferences: DEFAULT_PREFERENCES },
      "/config": { revision: 0, config: { showAiActivity: true, showTradeMarkers: false, showMarketBriefs: false, extensions: [] } },
      "/activity": { environment: "simulation", replayId: null, symbol: "SHFE.au2612", capturedAt: market.capturedAt, dataStatus: "normal", ageSeconds: 0, orders: [], strategies: [], protections: [], fills: [] },
      ...routes }).find(([key]) => path.endsWith(key));
    // A function answers by the whole URL (the desk report per ?symbol=), or with a promise held open.
    const value = hit && typeof hit[1] === "function" ? await (hit[1] as (url: URL) => unknown)(url) : hit?.[1];
    if (value instanceof Response) return value.clone();
    return new Response(JSON.stringify(hit ? value : { items: [] }), { headers: { "Content-Type": "application/json" } });
  }));
}
const rebar = { ...instruments.items[0], symbol: "SHFE.rb2610", instrumentId: "rb2610", instrumentName: "螺纹钢2610", productId: "rb", priceTick: 1, volumeMultiple: 10 };
const deskReport = {
  symbol: "SHFE.au2612", productName: "沪金", mainSymbol: "SHFE.au2612", generatedAt: "2026-10-04T01:30:00Z", sections: [], notice: "",
  facts: [{ id: "f1", section: "basis", kind: "data", label: "基差", value: "+38", source: "钢联", asOf: "2026-10-03" },
    { id: "f2", section: "stock", kind: "data", label: "社会库存", value: "512", source: "生意社", asOf: "2026-10-03" }],
  judgment: { status: "ready", today: true, stance: "偏多", confidence: "中", headline: "库存回落", generatedAt: "2026-10-04T01:30:00Z", researchDay: "2026-10-04",
    points: [{ text: "社会库存周降 4.2%", basis: ["f2"], kind: "推断", grounds: "data" }], risks: [{ text: "跌破 818", basis: [], kind: "推测", grounds: "data" }],
    basis: { f2: { section: "stock", kind: "data", label: "社会库存", value: "512", source: "生意社", asOf: "2026-10-03" } } },
};
const tabButton = (name: string) => within(screen.getByRole("navigation", { name: "主导航" })).getByRole("button", { name: new RegExp(`^${name}`) });

test("手机版调研：调研台手机版——立场与可能过时、4 格关键数据、两栏上下排、新闻面一行，其余折叠；相关快讯竖排；自选品种切换合约，就这份判断问 AI", async () => {
  stubPhone(true);
  const ai = { event: "价格", direction: "偏多", strength: "中", reason: "避险买盘推高金价", transmission: "避险买盘直接推高沪金", product: "au" };
  const report = (id: string, source: string, text: string, extra: Record<string, unknown> = {}) =>
    ({ id, source, sources: [source], publishedAt: "2026-10-04T02:00:00Z", title: "", text, link: null, tags: [], ...extra });
  // 第七批: the phone's 调研 is the desk in its phone form — four of the figures, the columns stacked, the 新闻面 line.
  const phoneDesk = { ...deskReport, news: { net: "偏多", take: "避险买盘推高金价", generatedAt: "2026-10-04T02:05:00Z" },
    judgment: { ...deskReport.judgment, stale: { count: 3, newsNet: null } },
    tiles: [["主力 AU2612", "M.price"], ["近 20 日", "M.trend"], ["COMEX黄金", "M.foreign"], ["日线 RSI", "M.indicators"], ["现货基差", "D.spot_basis"],
      ["美 10 年期国债", "E.rates"]].map(([label, ref]) => ({ label, value: "1", color: "none", note: "10-03", ref })) };
  phoneFetch({ "/api/research/desk": phoneDesk, "/api/preferences": { version: 1, preferences: { ...DEFAULT_PREFERENCES, watchlist: { products: ["SHFE.rb"] } } },
    "/api/research/flash": { status: "ok", items: [],
      events: [
        { id: "e1", eventLabel: "金价短线拉升", researchDay: "2026-10-04", publishedAt: "2026-10-04T02:00:00Z", firstAt: "2026-10-04T01:50:00Z", title: "", text: "金价短线拉升 0.6%",
          ai, tier: "major", reports: 2, sources: ["财联社", "金十"], direct: true, tags: [], items: [report("n1", "财联社", "金价短线拉升 0.6%"), report("n3", "金十", "金价拉升")] },
        { id: "e2", eventLabel: "ETF 持仓", researchDay: "2026-10-04", publishedAt: "2026-10-04T01:30:00Z", firstAt: "2026-10-04T01:30:00Z", title: "", text: "黄金 ETF 持仓小增",
          ai: { ...ai, direction: "偏多", strength: "弱" }, tier: "other", reports: 1, sources: ["新浪"], direct: true, tags: [],
          items: [report("n4", "新浪", "黄金 ETF 持仓小增", { origTitle: "Gold ETF holdings edge higher" })] },
        { id: "e3", eventLabel: "欧央行官员表态", researchDay: "2026-10-04", publishedAt: "2026-10-04T01:20:00Z", firstAt: "2026-10-04T01:20:00Z", title: "", text: "欧央行委员例行表态",
          ai: { ...ai, direction: "中性", strength: "弱" }, tier: "minor", reports: 1, sources: ["新浪"], direct: true, tags: [], items: [report("n5", "新浪", "欧央行委员例行表态")] },
        // 上期所通知 + 翻译过的英文报道（两条）：事件带 交易所 徽标；手机上报道列表不展开，
        // 标题行自己折叠英文原文，正文（=原文）不重复显示。
        { id: "e4", eventLabel: "铜价上涨", researchDay: "2026-10-04", publishedAt: "2026-10-04T01:15:00Z", firstAt: "2026-10-04T01:15:00Z", title: "上期所上调铜期货保证金",
          text: "SHFE raises copper margin requirements", origTitle: "SHFE raises copper margin requirements",
          ai: { ...ai, strength: "弱" }, tier: "major", reports: 2, sources: ["上期所", "FinancialJuice"], direct: true, tags: ["交易所"],
          items: [report("n6", "上期所", "上期所上调铜期货保证金", { tags: ["交易所"], publishedAt: "2026-10-04T01:15:00Z", title: "上期所上调铜期货保证金" }),
            report("n7", "FinancialJuice", "SHFE raises copper margin requirements", { publishedAt: "2026-10-04T01:16:00Z",
              title: "上期所上调铜期货保证金", origTitle: "SHFE raises copper margin requirements" })] }],
      layers: { major: 2, other: 1, minor: 1, unscreened: 0, minorReports: 1, minorGroups: [{ label: "欧央行官员表态", count: 1 }] },
      brief: { status: "ready", net: "偏多", take: "避险买盘推高金价，新闻面偏多。", basedOn: 3, generatedAt: "2026-10-04T02:05:00Z",
        points: [{ text: "金价短线拉升：避险买盘推高沪金", direction: "偏多", eventLabel: "金价短线拉升", eventId: "e1", reports: 2 }], watch: ["今晚 20:30 非农"] },
      screen: { mode: "ai", pending: 0, dropped: { count: 1, groups: [{ reason: "黄金周等同名说法", count: 1 }], items: [
        { id: "n2", source: "新浪", sources: ["新浪"], publishedAt: "2026-10-04T01:00:00Z", title: "", text: "黄金周出游人次创新高", link: null, products: ["au"], tags: [], dropReason: "黄金周等同名说法", droppedBy: "keyword" }] } } } });
  const values = props({ runtimeBaseUrl: "https://phone.example.test", instruments: { ...instruments, items: [...instruments.items, rebar] } });
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(tabButton("调研"));
  const page = () => screen.getByTestId("mobile-research");
  const verdict = await within(page()).findByRole("region", { name: "今日判断" });
  expect(verdict).toHaveTextContent("库存回落 · 09:30之后又来了 3 条重要消息，可能过时重新判断");
  // The sample's four figures: the price, the foreign one, RSI and the macro one.
  expect(within(within(page()).getByLabelText("关键数据")).getAllByRole("button").map(button => button.getAttribute("aria-label")))
    .toEqual(["主力 AU2612 1，看来源", "COMEX黄金 1，看来源", "日线 RSI 1，看来源", "美 10 年期国债 1，看来源"]);
  // An old judgment (no directions): the points go with its 偏多, the risks to 偏空; no numbers on the phone.
  expect(page().querySelector('[data-side="bull"]')).toHaveTextContent("偏多社会库存周降 4.2%");
  expect(page().querySelector('[data-side="bear"]')).toHaveTextContent("偏空跌破 818");
  expect(within(page()).queryByRole("button", { name: /^依据/ })).toBeNull();
  expect(within(page()).getByText("新闻面偏多")).toBeVisible();
  expect(within(page()).getByRole("button", { name: "快讯 →" })).toBeVisible();
  expect(within(page()).getByRole("button", { name: "展开资料来源" })).toHaveTextContent("2 项");
  await waitFor(() => expect(within(page()).getByText(/金价短线拉升 0\.6%/)).toBeVisible());
  // 第六批: the compact 要点 (direction, one sentence, points — no meta, no 接下来关注), the 重要 event with its
  // direction · strength and outlet count, and one fold for 其他 / 次要 / 无关.
  const brief = within(page()).getByRole("region", { name: "今日新闻要点" });
  expect(within(brief).getByText("偏多")).toHaveAttribute("data-tone", "up");
  expect(within(brief).getByText("避险买盘推高金价，新闻面偏多。")).toBeVisible();
  expect(within(brief).queryByText(/AI 新闻分析师/)).toBeNull();
  expect(within(brief).queryByText("接下来关注")).toBeNull();
  expect(within(page()).getByLabelText("偏多，强度中")).toHaveTextContent("偏多 · 中");
  expect(within(page()).getAllByText("2 家报道")).toHaveLength(2);  // 金价与铜价两个双报事件
  // The exchange-notice event with a translated English report: the 交易所 badge, the Chinese
  // headline, the original folded behind its own 原文 toggle (flash-orig, the event's id on the
  // phone), and the body (identical to the original) never a second copy.
  expect(within(page()).getByText("上期所上调铜期货保证金")).toBeVisible();
  expect(page().querySelector(".news-ev-tag")).toHaveTextContent("交易所");
  expect(within(page()).queryByText(/SHFE raises copper margin requirements/)).toBeNull();  // 折叠着，不显示
  fireEvent.click(within(page()).getByRole("button", { name: "原文 展开" }));
  expect(within(page()).getByText("原文：SHFE raises copper margin requirements")).toBeVisible();
  expect(within(page()).getAllByText(/SHFE raises copper margin requirements/)).toHaveLength(1);
  expect(uiSurface("disclosure:flash-orig:e4")?.state).toBe(true);
  expect(within(page()).queryByText(/黄金 ETF 持仓小增/)).toBeNull();
  expect(within(page()).getByText(/次要/).closest(".news-fold-row")).toHaveTextContent("其他 1 条 · 次要 1 条 · 无关 1 条");
  fireEvent.click(within(page()).getByRole("button", { name: "展开次要消息" }));
  expect(within(page()).getByText(/黄金 ETF 持仓小增/)).toBeVisible();
  // A single translated report inside the fold: its original folds under the headline too.
  fireEvent.click(within(page()).getByRole("button", { name: "原文 展开" }));
  expect(within(page()).getByText("原文：Gold ETF holdings edge higher")).toBeVisible();
  expect(within(page()).getByText(/欧央行委员例行表态/)).toBeVisible();
  expect(within(page()).getByText(/AI 筛掉了/)).toHaveTextContent("AI 筛掉了 1 条无关");
  expect(within(page()).queryByText("黄金周出游人次创新高")).toBeNull();
  fireEvent.click(within(page()).getByRole("button", { name: "查看" }));
  expect(within(page()).getByText("黄金周出游人次创新高")).toBeVisible();
  const chips = within(page()).getByRole("group", { name: "调研品种" });
  await waitFor(() => expect(within(chips).getAllByRole("button").map(button => button.textContent)).toEqual(["沪金", "螺纹钢", "自选"]));
  expect(within(chips).getByRole("button", { name: "沪金" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(within(chips).getByRole("button", { name: "螺纹钢" }));
  expect(values.onSelectSymbol).toHaveBeenCalledWith("SHFE.rb2610");
  fireEvent.click(within(page()).getByRole("button", { name: "就这份判断问 AI" }));
  expect(screen.getByRole("region", { name: "AI 对话" })).toBeVisible();
  await waitFor(() => expect(values.onPrompt).toHaveBeenCalled());
  expect((values.onPrompt.mock.calls[0] as unknown[])[0]).toContain("沪金今天这份判断（库存回落）");
});

test("手机版 AI 首页与调研：刷新失败时保留内容并在旁边说一行，恢复后消失，换身份不残留 (#516)", async () => {
  stubPhone(true);
  let failing = false;
  const brief = { id: "b1", kind: "news", text: "沪金简讯：美元走弱", source: "fixture", product: "SHFE.au", productName: "沪金",
    publishedAt: new Date(Date.now() - 60_000).toISOString(), expiresAt: new Date(Date.now() + 3_600_000).toISOString() };
  const flashItem = { id: "n1", source: "财联社", sources: ["财联社"], publishedAt: new Date().toISOString(), title: "", text: "金价短线拉升 0.6%", link: null, products: ["au"], tags: [] };
  phoneFetch({
    "/config": { revision: 0, config: { showAiActivity: true, showTradeMarkers: false, showMarketBriefs: true, extensions: [] } },
    "/api/app/briefs": () => failing ? new Response("{}", { status: 503 }) : { items: [brief] },
    "/api/research/flash": () => failing ? new Response("{}", { status: 502 }) : { status: "ok", items: [flashItem] },
  });
  render(<FuturesDecisionWorkspace {...props({ runtimeBaseUrl: "https://phone.example.test" }) as any} />);
  const home = () => screen.getByTestId("mobile-feed");
  await waitFor(() => expect(within(home()).getByText(/美元走弱/)).toBeInTheDocument());
  expect(within(home()).queryByTestId("mobile-feed-refresh-error")).toBeNull();
  failing = true;
  await act(async () => { await readQueryClient.refetchQueries(); });
  await waitFor(() => expect(within(home()).getByTestId("mobile-feed-refresh-error")).toHaveTextContent("简讯刷新失败，显示上次的 1 条"));
  expect(within(home()).getByText(/美元走弱/)).toBeInTheDocument();          // the kept rows stay
  failing = false;
  await act(async () => { await readQueryClient.refetchQueries(); });
  await waitFor(() => expect(within(home()).queryByTestId("mobile-feed-refresh-error")).toBeNull());
  failing = true;
  act(() => resetReadIdentity());                                             // another login: neither old rows nor the error
  await waitFor(() => expect(within(home()).getByTestId("mobile-feed-refresh-error")).toHaveTextContent("简讯读取失败，稍后自动重试"));
  expect(within(home()).queryByText(/美元走弱/)).toBeNull();

  // 调研: the 快讯 kept on a failed 下拉刷新, with the same one line; a good read clears it.
  failing = false;
  fireEvent.click(tabButton("调研"));
  const page = () => screen.getByTestId("mobile-research");
  await waitFor(() => expect(within(page()).getByText(/金价短线拉升/)).toBeVisible());
  const pull = async () => {
    const content = page().querySelector(".fdm-content")!;
    fireEvent.touchStart(content, { touches: [{ clientX: 10, clientY: 10 }] });
    fireEvent.touchMove(content, { touches: [{ clientX: 10, clientY: 20 }] });
    fireEvent.touchMove(content, { touches: [{ clientX: 10, clientY: 220 }] });
    await act(async () => { fireEvent.touchEnd(content); await new Promise(resolve => setTimeout(resolve, 20)); });
  };
  failing = true;
  await pull();
  await waitFor(() => expect(within(page()).getByTestId("research-flash-refresh-error")).toHaveTextContent("快讯刷新失败，显示上次的内容"));
  expect(within(page()).getByText(/金价短线拉升/)).toBeVisible();
  failing = false;
  await pull();
  await waitFor(() => expect(within(page()).queryByTestId("research-flash-refresh-error")).toBeNull());
});

test("手机版调研与看盘：读取失败与没有合约时各有说明", async () => {
  stubPhone(true);
  phoneFetch({ "/api/research/desk": new Response("{}", { status: 502 }), "/api/research/flash": new Response("{}", { status: 502 }) });
  const view = render(<FuturesDecisionWorkspace {...props({ runtimeBaseUrl: "https://phone.example.test" }) as any} />);
  fireEvent.click(tabButton("调研"));
  expect(await screen.findByText("调研暂不可用（HTTP 502），约 4 秒后自动重试。")).toBeVisible();
  expect(await screen.findByText("快讯暂不可用，稍后自动重试。")).toBeVisible();
  view.unmount();
  render(<FuturesDecisionWorkspace {...props({ market: undefined, selectedSymbol: undefined, account: { ...account, positions: [] }, marketError: "当前行情暂时不可用" }) as any} />);
  fireEvent.click(tabButton("调研"));
  expect(screen.getByText("还没有选择合约")).toBeVisible();
  fireEvent.click(tabButton("看盘"));
  const market = screen.getByTestId("mobile-market");
  expect(within(market).getByText("当前行情暂时不可用")).toBeVisible();
  expect(within(market).getByRole("button", { name: "下单" })).toBeDisabled();
  expect(within(market).getByText(/还没有自选或持仓/)).toBeVisible();
});

test("手机版看盘：新浪备用行情时价格旁同样标“备用 · 新浪”", () => {
  stubPhone(true);
  const backup = { ...market, dataStatus: "backup" as const, source: "新浪（备用）", quote: { ...market.quote, dataStatus: "backup" as const } };
  render(<FuturesDecisionWorkspace {...props({ market: backup, simulationEnabled: false }) as any} />);
  fireEvent.click(tabButton("看盘"));
  expect(within(screen.getByTestId("mobile-market")).getByTestId("quote-backup")).toHaveTextContent("备用 · 新浪");
});

test("手机版策略编辑卡：strategyEditor 命令在手机布局打开整屏编辑页（回执 applied、快照照常），底部标签隐藏；返回后标签回来", async () => {
  stubPhone(true);
  let readTrades: () => void = () => undefined;
  const trades = new Promise<void>(resolve => { readTrades = resolve; });
  phoneFetch({ "/api/strategy-plans/prefill": async () => { await trades; return { eligible: false, reason: "成交不够", sample: { trades: 1, minTrades: 5 }, fields: {}, trades: [] }; },
    "/api/strategy-plans": { items: [], executions: [] }, "/api/strategy-templates": { categories: [], items: [] } });
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const view = render(<FuturesDecisionWorkspace {...props({ runtimeBaseUrl: "https://phone.example.test" }) as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  const apply = (changes: Record<string, unknown>) => act(() => { control.mock.calls.at(-1)?.[3](changes as any); });
  try {
    apply({ strategyEditor: { new: true } });
    expect(await screen.findByTestId("mobile-strategy-editor")).toBeVisible();
    expect(document.querySelector("nav.fdm-tabs")).toHaveAttribute("hidden");
    // 线上验收 (2026-10-08): while the trades load, `new` is not applied yet — the state is still about to change, and
    // a command sent on an early receipt was refused as stale. Once loaded, the next command lands at once.
    await waitFor(() => expect(state()).toMatchObject({ strategyEditor: { open: true, loading: "正在读你的成交…" } }));
    await act(async () => { readTrades(); });
    await waitFor(() => expect(state()).toMatchObject({ mobileLayout: true, strategyEditor: { open: true, mode: "new", start: "blank", loading: null } }));
    const settled = JSON.stringify(state());
    apply({ disclosure: { name: "strategy-editor-section", id: "entry", open: true } });
    expect((state() as any).disclosures["strategy-editor-section:entry"]).toBe(true);
    // Nothing else moved the window's state after `new` was applied (no stale-revision refusal for the next command).
    expect(JSON.stringify({ ...state(), disclosures: undefined })).toBe(JSON.stringify({ ...JSON.parse(settled), disclosures: undefined }));
    apply({ disclosure: { name: "strategy-editor-section", id: "exit", open: true } });
    expect(screen.getByRole("button", { name: "平仓" })).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByRole("button", { name: "返回我的策略" }));
    await waitFor(() => expect(screen.queryByTestId("mobile-strategy-editor")).toBeNull());
    expect(screen.getByRole("navigation", { name: "主导航" })).toBeVisible();
    expect(state()).toMatchObject({ strategyEditor: { open: false } });
  } finally {
    view.unmount(); control.mockRestore(); vi.unstubAllGlobals();
  }
});

test("手机版看盘：周期与合约切换、持仓标记行切图、下单进入下单页；mobilePage 与快照一致", () => {
  stubPhone(true);
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const values = props();
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  try {
    fireEvent.click(tabButton("看盘"));
    const page = screen.getByTestId("mobile-market");
    expect(within(page).getByRole("button", { name: "选择合约：沪金2612" })).toBeVisible();
    expect(within(within(page).getByRole("group", { name: "周期" })).getAllByRole("button").map(button => button.textContent)).toEqual(["分时", "5分", "15分", "1小时", "日线"]);
    expect(within(page).getByRole("button", { name: "5分" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(page).getByRole("button", { name: "日线" }));
    expect(values.onDurationChange).toHaveBeenLastCalledWith(86400);
    fireEvent.click(within(page).getByRole("button", { name: "分时" }));
    expect(state()).toMatchObject({ chartView: "timeshare" });
    // 自选 · 持仓标记: the held contract carries its side and size; a tap switches the chart.
    const row = within(page).getByRole("button", { name: "看 沪金2612" });
    expect(row).toHaveTextContent("多2");
    fireEvent.click(row);
    expect(values.onSelectSymbol).toHaveBeenCalledWith("SHFE.au2612");
    fireEvent.click(within(page).getByRole("button", { name: "选择合约：沪金2612" }));
    expect(screen.getByRole("dialog", { name: "合约列表" })).toBeVisible();
    expect(state()).toMatchObject({ dialog: "contract-drawer" });
    act(() => { control.mock.calls.at(-1)?.[3]({ dialog: { name: "contract-drawer", open: false } } as any); });
    fireEvent.click(within(page).getByRole("button", { name: "下单" }));
    expect(screen.getByTestId("mobile-order")).toBeVisible();
    expect(state()).toMatchObject({ mobileTab: "market", mobilePage: "order" });
  } finally {
    view.unmount(); control.mockRestore();
  }
});

test("手机看盘：mobileMarket 命令先切到看盘再改报价 / 单图，写进账号偏好后回执 applied；点报价行进那个合约的单图", async () => {
  stubPhone(true);
  let document = { version: 1, preferences: DEFAULT_PREFERENCES };
  const marketBoard = { capturedAt: "", dataStatus: "normal", revision: "1", truncated: false, rows: [{
    symbol: "SHFE.rb2610", exchangeId: "SHFE", productId: "rb", instrumentId: "rb2610", instrumentName: "螺纹钢2610", productName: "螺纹钢",
    last: 3076, preSettlement: 3100, change: -24, changePercent: -0.77, volume: 1, openInterest: 2_100_000, openInterestChange: 0,
    high: null, low: null, upperLimit: null, lowerLimit: null, quoteTime: "", status: "normal" }] };
  phoneFetch({ "/api/market/board": marketBoard });
  const others = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (input: Request | string, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : input);
    if (!url.pathname.endsWith("/api/preferences")) return others(input, init);
    if (init?.method === "PUT") {
      const patch = JSON.parse(String(init.body)) as { ui?: Record<string, unknown> };
      const ui = { ...document.preferences.ui!, mobileMarket: { ...document.preferences.ui?.mobileMarket, ...(patch.ui?.mobileMarket as object) } };
      document = { version: document.version + 1, preferences: { ...document.preferences, ui } };
    }
    return new Response(JSON.stringify(document), { headers: { "Content-Type": "application/json" } });
  }));
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const values = props({ runtimeBaseUrl: "https://phone.example.test", instruments: { ...instruments, items: [...instruments.items, rebar] } });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  const apply = (changes: Record<string, unknown>) => act(() => { control.mock.calls.at(-1)?.[3](changes as any); });
  try {
    expect(state()).toMatchObject({ mobileTab: "ai" });
    apply({ mobileMarket: { view: "quotes" } });
    expect(state()).toMatchObject({ mobileTab: "market" });
    await waitFor(() => expect((state() as any).preferences.ui.mobileMarket).toMatchObject({ view: "quotes" }));
    apply({ mobileMarket: { board: "main" } });
    await waitFor(() => expect((state() as any).preferences.ui.mobileMarket).toMatchObject({ view: "quotes", board: "main" }));
    const row = await screen.findByRole("button", { name: "看 螺纹钢2610 单图" });
    expect(row).toHaveTextContent("-0.77%");
    fireEvent.click(row);
    expect(values.onSelectSymbol).toHaveBeenCalledWith("SHFE.rb2610");
    await waitFor(() => expect((state() as any).preferences.ui.mobileMarket).toMatchObject({ view: "single" }));
    expect(screen.getByRole("group", { name: "周期" })).toBeVisible();
    // The 副图设置 sheet: a command opens it on the single chart, reported as its disclosure.
    apply({ mobileMarket: { subPanes: true } });
    expect(screen.getByRole("dialog", { name: "副图设置" })).toBeVisible();
    expect((state() as any).disclosures["mobile-subpanes"]).toBe(true);
  } finally {
    view.unmount(); control.mockRestore(); vi.unstubAllGlobals();
  }
});

test("手机版下单页：对手价起步、价格与手数步进，确认走现有确认下单接口；取消不下单", async () => {
  stubPhone(true);
  const values = props({ onManualSubmit: vi.fn(async () => undefined) });
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(tabButton("看盘"));
  fireEvent.click(screen.getByRole("button", { name: "下单" }));
  const sheet = screen.getByTestId("mobile-order");
  expect(within(sheet).getByRole("button", { name: /买多\s*卖一 824\.02/ })).toBeVisible();
  expect(within(sheet).getByRole("button", { name: "确认买多 1 手 @ 824.02" })).toBeEnabled();
  fireEvent.click(within(sheet).getByRole("button", { name: "手数加一" }));
  fireEvent.click(within(sheet).getByRole("button", { name: /卖空/ }));
  expect(within(sheet).getByRole("button", { name: "确认卖空 2 手 @ 823.98" })).toBeVisible();
  fireEvent.click(within(sheet).getByRole("button", { name: "限价" }));
  fireEvent.click(within(sheet).getByRole("button", { name: "委托价加一跳" }));
  fireEvent.click(within(sheet).getByRole("button", { name: "确认卖空 2 手 @ 824.02" }));
  await waitFor(() => expect(values.onManualSubmit).toHaveBeenCalledWith(expect.objectContaining({ side: "sell_open", volume: 2, priceIntent: "passive", limitPrice: 824.02 })));
  // Nothing was sent (the mocked submit declines): the sheet says so in the desktop's words.
  expect(await within(sheet).findByTestId("mobile-order-result")).toHaveTextContent("委托结果：未发送");
  fireEvent.click(within(sheet).getByRole("button", { name: "关闭下单" }));
  expect(screen.queryByTestId("mobile-order")).toBeNull();
  // Opening it again and closing without a confirm sends nothing.
  fireEvent.click(screen.getByRole("button", { name: "下单" }));
  fireEvent.click(screen.getByRole("button", { name: "关闭下单" }));
  expect(values.onManualSubmit).toHaveBeenCalledTimes(1);
});

test("#932 A26 手机下单页：一次确认下了今昨两笔，结果写总手数并逐笔列出状态、手数和价格（以前只报第一笔）", async () => {
  stubPhone(true);
  const child = (clientOrderId: string, offset: string, volume: number, displayStatus: string) => ({ id: clientOrderId, clientOrderId, displayStatus,
    proposalId: `p-${clientOrderId}`, request: { symbol: "SHFE.au2612", direction: "SELL", offset, volume, priceType: "LIMIT", limitPrice: 823.98 } });
  const values = props({ onManualSubmit: vi.fn(async () => ({ confirmed: true, accepted: true, orders: [child("T", "CLOSETODAY", 2, "已受理"), child("H", "CLOSE", 1, "已成交")],
    skipped: [{ volume: 1, label: "au2612 无最新报价" }] })) });
  render(<FuturesDecisionWorkspace {...values as any} />);
  fireEvent.click(tabButton("看盘"));
  fireEvent.click(screen.getByRole("button", { name: "下单" }));
  const sheet = screen.getByTestId("mobile-order");
  fireEvent.click(within(sheet).getByRole("button", { name: "确认买多 1 手 @ 824.02" }));
  const result = await within(sheet).findByTestId("mobile-order-result");
  await waitFor(() => expect(result).toHaveTextContent("委托结果：共 3 手 · 2 笔"));
  expect(result).toHaveTextContent("卖平今 2 手 @ 823.98：已受理");
  expect(result).toHaveTextContent("卖平昨 1 手 @ 823.98：已成交");
  expect(result).toHaveTextContent("另 1 手未纳入：au2612 无最新报价");
  expect(result).toHaveAttribute("data-status", "mixed");
});

test("手机版提醒（统一提醒 #932）：AI 标签与铃铛带服务端的未读数，铃铛进整屏提醒页，首页有最新提醒；点一条与全部已读写服务端已读；时间线只剩成交与简报，不再记本机已读", async () => {
  stubPhone(true);
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const posts: unknown[] = [];
  let unread = 2;
  const reminder = (id: string, clock: string, over: Record<string, unknown> = {}) => ({ id, kind: "price_alert", tradingDay: "2026-10-04",
    createdAt: `2026-10-04T${clock}:00+08:00`, name: "沪金2612", title: "到价提醒", body: "沪金2612 到了 824", read: false,
    target: { type: "chat", cardId: "r1" }, ...over });
  vi.stubGlobal("fetch", vi.fn(async (input: Request | string, init?: RequestInit) => {
    const path = new URL(input instanceof Request ? input.url : input).pathname;
    const body = (data: unknown) => new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
    if (path.endsWith("/api/reminders/read")) {
      const wanted = JSON.parse(String(init?.body));
      posts.push(wanted);
      unread = wanted.all ? 0 : unread - wanted.ids.length;
      return body({ unread });
    }
    // An older Runtime (no moreCommand) with a day past its pages: today keeps a next cursor, later pages are empty.
    const query = new URL(input instanceof Request ? input.url : input).searchParams;
    const busyToday = query.get("scope") === "today";
    if (path.endsWith("/api/reminders") && busyToday && query.get("before")) return body({ unread, tradingDay: "2026-10-04", nextCursor: "more", items: [] });
    if (path.endsWith("/api/reminders")) return body({ unread, tradingDay: "2026-10-04", nextCursor: busyToday ? "more" : null, items: [
      reminder("a", "10:42"), reminder("b", "09:58", { kind: "fill", title: "成交", body: "买多 1 手 @ 823", target: { type: "orders" } })] });
    if (path.endsWith("/api/preferences")) return body({ version: 1, preferences: DEFAULT_PREFERENCES });
    if (path.endsWith("/config")) return body({ revision: 0, config: { showAiActivity: true, showTradeMarkers: false, showMarketBriefs: false, extensions: [] } });
    if (path.endsWith("/activity")) return body({ environment: "simulation", replayId: null, symbol: "SHFE.au2612", capturedAt: market.capturedAt,
      dataStatus: "normal", ageSeconds: 0, orders: [], strategies: [], protections: [], fills: [] });
    return body({ items: [] });
  }));
  const values = props({ runtimeBaseUrl: "https://reminders.example.test", state: { turns: phoneTurns, runtime: { simulationTrading: true } } });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  const apply = (changes: Record<string, unknown>) => act(() => { control.mock.calls.at(-1)?.[3](changes as any); });
  const home = () => screen.getByTestId("mobile-feed");
  try {
    await waitFor(() => expect(tabButton("AI")).toHaveAccessibleName("AI（2 条未读）"));
    expect(screen.getByTestId("notify-dot")).toHaveTextContent("2");
    expect(state()).toMatchObject({ mobileUnread: 2, reminderCenter: { open: false, filter: "all", unread: 2, today: 2 } });
    // Old Runtime → the snapshot keeps the old shape, even on a day with more to read (Codex r4308078551): its
    // ReminderCenterState forbids the #932 R5 fields, and the `more` command is refused here instead.
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([input]) => String(input instanceof Request ? input.url : input).includes("before=more")).length).toBe(9));
    await act(async () => { await Promise.resolve(); });
    expect(Object.keys((state() as { reminderCenter: object }).reminderCenter).sort()).toEqual(["earlier", "filter", "open", "today", "unread"]);
    expect(() => control.mock.calls.at(-1)?.[3]({ reminderCenter: { more: true } } as any)).toThrow("这个服务端版本还不支持继续加载当天提醒的命令");
    // The timeline carries the fill and the judgment only, with no read marks of its own.
    expect(within(home()).getByRole("group", { name: "时间线筛选" })).toHaveTextContent("全部成交AI 简报");
    expect(within(within(home()).getByRole("list", { name: "时间线" })).getAllByRole("listitem")).toHaveLength(2);
    expect(within(home()).queryByRole("button", { name: "全部已读" })).toBeNull();
    expect(Object.keys(localStorage).some(key => key.startsWith("ai-trader:mobile-notify-read:"))).toBe(false);
    // 最新提醒: a row is read on the Runtime and opens its card in the conversation.
    fireEvent.click(within(within(home()).getByRole("region", { name: "最新提醒" })).getByRole("button", { name: /^到价 沪金2612/ }));
    expect(screen.getByRole("region", { name: "AI 对话" })).toBeVisible();
    await waitFor(() => expect(posts).toEqual([{ ids: ["a"] }]));
    await waitFor(() => expect(tabButton("AI")).toHaveAccessibleName("AI（1 条未读）"));
    // The bell: the full-screen 提醒 page; 全部已读 there clears the badge everywhere.
    fireEvent.click(tabButton("AI"));
    fireEvent.click(screen.getByRole("button", { name: "看提醒（1 条未读）" }));
    const page = screen.getByTestId("mobile-reminders");
    expect(state()).toMatchObject({ mobilePage: "reminders", reminderCenter: { open: true } });
    fireEvent.click(within(page).getByRole("button", { name: "全部已读" }));
    await waitFor(() => expect(posts.at(-1)).toEqual({ all: true }));
    expect(screen.queryByTestId("notify-dot")).toBeNull();
    // A fill reminder opens 持仓与委托 at 委托.
    fireEvent.click(within(page).getByRole("button", { name: /^成交 沪金2612/ }));
    expect(state()).toMatchObject({ mobilePage: "positions", positionsTab: "orders" });
    // Commands: the retired 提醒 filter and reminderCenter both reach the page; reminderCenter closes it.
    apply({ mobilePage: null });
    apply({ mobileNotifyFilter: "reminder" });
    expect(state()).toMatchObject({ mobilePage: "reminders" });
    apply({ reminderCenter: { open: false } });
    expect(state()).toMatchObject({ mobilePage: null, reminderCenter: { open: false } });
    apply({ reminderCenter: { filter: "unread" } });
    expect(state()).toMatchObject({ mobilePage: "reminders", reminderCenter: { open: true, filter: "unread" } });
  } finally {
    view.unmount(); control.mockRestore(); vi.unstubAllGlobals();
  }
});

test("手机版 AI 首页按订阅品种：今日判断胶囊与简报品种胶囊列推送范围内的订阅品种（再接时间线里出现的），每个一条今日判断（与当前合约去重），点胶囊到调研并切合约；无内容的订阅品种显示空态；订阅读不到退回旧行为", async () => {
  stubPhone(true);
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const subscribed = (product: string, name: string, symbol: string) => ({ product, name, symbol });
  const judged = (symbol: string, productName: string, headline: string) => ({ ...deskReport, symbol, mainSymbol: symbol, productName,
    judgment: { ...deskReport.judgment, headline } });
  const desks: Record<string, unknown> = { "SHFE.rb2601": judged("SHFE.rb2601", "螺纹钢", "螺纹去库"), "CZCE.AP601": judged("CZCE.AP601", "苹果", "苹果走货"),
    "SHFE.au2612": judged("SHFE.au2612", "沪金", "金价偏强"), "SHFE.ru2601": new Response("{}", { status: 502 }) };
  // The chart's contract may come upper-cased; it is still the subscribed 沪金.
  const values = props({ runtimeBaseUrl: "https://subs.example.test", state: { turns: phoneTurns, runtime: { simulationTrading: true } },
    market: { ...market, quote: { ...market.quote, symbol: "SHFE.AU2612" } } });
  const desk = vi.fn((url: URL) => desks[url.searchParams.get("symbol") ?? ""] ?? new Response("{}", { status: 404 }));
  // 沪金 (the chart's product) is subscribed; 铁矿石 is past the tier's cap of 4 and not pushed.
  phoneFetch({ "/api/research/desk": desk, "/api/app/brief-subscriptions": { limit: 4, source: "user", overLimit: true, products: [
    subscribed("SHFE.rb", "螺纹钢", "SHFE.rb2601"), subscribed("CZCE.AP", "苹果", "CZCE.AP601"), subscribed("SHFE.ru", "橡胶", "SHFE.ru2601"),
    subscribed("SHFE.au", "沪金", "SHFE.au2612"), subscribed("DCE.i", "铁矿石", "DCE.i2601")] } });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  const apply = (changes: Record<string, unknown>) => act(() => { control.mock.calls.at(-1)?.[3](changes as any); });
  try {
    // One read per pushed product (铁矿石 is past the cap); 沪金 is read before the subscriptions arrive.
    const read = () => [...new Set(desk.mock.calls.map(([url]) => url.searchParams.get("symbol")))].sort();
    await waitFor(() => expect(read()).toEqual(["CZCE.AP601", "SHFE.AU2612", "SHFE.au2612", "SHFE.rb2601", "SHFE.ru2601"]));
    // 今日判断: one chip per pushed product in the subscription order, 暂无 until a ready judgment
    // (橡胶's read failed); 调研 shows the subscribed read of the chart's product.
    const judges = () => within(screen.getByRole("group", { name: "今日判断" })).getAllByRole("button").map(button => button.getAttribute("aria-label"));
    await waitFor(() => expect(judges()).toEqual(["螺纹钢 今日判断", "苹果 今日判断", "橡胶 暂无", "沪金 今日判断"]));
    expect(within(screen.getByTestId("mobile-feed")).queryByTestId("research-judgment-card")).toBeNull();
    fireEvent.click(tabButton("调研"));
    expect(await within(screen.getByTestId("mobile-research")).findByText(/金价偏强/)).toBeVisible();
    fireEvent.click(tabButton("AI"));
    fireEvent.click(screen.getByRole("button", { name: "螺纹钢 今日判断" }));
    expect(values.onSelectSymbol).toHaveBeenLastCalledWith("SHFE.rb2601");
    expect(screen.getByTestId("mobile-research")).toBeVisible();
    fireEvent.click(tabButton("AI"));
    const page = screen.getByTestId("mobile-feed");
    fireEvent.click(within(page).getByRole("button", { name: "AI 简报" }));
    const chips = within(page).getByRole("group", { name: "简报品种" });
    expect(within(chips).getAllByRole("button").map(button => button.textContent)).toEqual(["全部品种", "螺纹钢", "苹果", "橡胶", "沪金"]);
    const list = () => within(within(page).getByRole("list", { name: "时间线" }));
    await waitFor(() => expect(list().getAllByRole("listitem").map(item => item.textContent).join("|")).toContain("螺纹去库"));
    const judgments = list().getAllByRole("listitem").map(item => item.textContent ?? "");
    expect(judgments).toHaveLength(3);
    expect(judgments.filter(text => text.includes("沪金"))).toHaveLength(1);
    expect(judgments.join("|")).toContain("苹果走货");
    // 橡胶 has nothing yet (no brief, its judgment unread): the page says so.
    fireEvent.click(within(chips).getByRole("button", { name: "橡胶" }));
    expect(within(page).queryByRole("list", { name: "时间线" })).toBeNull();
    expect(within(page).getByText("橡胶还没有简报")).toBeVisible();
    expect(within(page).getByText("有官方公告或行情异动时会推送到这里")).toBeVisible();
    // The command takes a subscribed product (with or without rows); 铁矿石 is neither pushed nor in 通知.
    apply({ mobileNotifyProduct: "CZCE.AP" });
    expect(state()).toMatchObject({ mobileNotifyProduct: "CZCE.AP", mobileNotifyFilter: "brief", mobileTab: "ai" });
    expect(list().getAllByRole("listitem")).toHaveLength(1);
    expect(() => apply({ mobileNotifyProduct: "DCE.i" })).toThrow("没有订阅这个品种，时间线里也没有它");
    // A subscribed product's judgment opens 调研 on that product.
    fireEvent.click(list().getByRole("button", { name: /苹果/ }));
    expect(values.onSelectSymbol).toHaveBeenCalledWith("CZCE.AP601");
    expect(screen.getByTestId("mobile-research")).toBeVisible();
  } finally {
    view.unmount(); control.mockRestore();
  }
  // Subscriptions unreadable: the chips are the products 通知 carries, as before (the chart's judgment).
  phoneFetch({ "/api/research/desk": deskReport, "/api/app/brief-subscriptions": new Response("{}", { status: 502 }) });
  render(<FuturesDecisionWorkspace {...props({ runtimeBaseUrl: "https://subs-failed.example.test" }) as any} />);
  fireEvent.click(within(screen.getByTestId("mobile-feed")).getByRole("button", { name: "AI 简报" }));
  await waitFor(() => expect(within(screen.getByRole("list", { name: "时间线" })).getAllByRole("listitem")).toHaveLength(1));
  expect(within(screen.getByRole("group", { name: "简报品种" })).getAllByRole("button").map(button => button.textContent)).toEqual(["全部品种", "沪金"]);
});

test("保护：订阅清单还没读回时，今日判断胶囊说“读取中”、用品种中文名，不显示“暂无”或代码（10-07 线上刚打开闪过“CZCE.AP 暂无”）", async () => {
  stubPhone(true);
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const apple = { ...instruments.items[0], symbol: "CZCE.AP701", exchangeId: "CZCE", instrumentId: "AP701", instrumentName: "苹果701", productId: "AP" };
  // A brief that carries only the product code (no name), as the merged briefs may.
  const brief = { id: "b1", kind: "news", text: "苹果简讯：产区走货加快", source: "fixture", product: "CZCE.AP",
    publishedAt: new Date(Date.now() - 60_000).toISOString(), expiresAt: new Date(Date.now() + 3_600_000).toISOString() };
  let release!: () => void;
  const held = new Promise<void>(done => { release = done; });
  phoneFetch({
    "/config": { revision: 0, config: { showAiActivity: true, showTradeMarkers: false, showMarketBriefs: true, extensions: [] } },
    "/api/app/briefs": { items: [brief] },
    "/api/research/desk": (url: URL) => url.searchParams.get("symbol") === "CZCE.AP701"
      ? { ...deskReport, symbol: "CZCE.AP701", mainSymbol: "CZCE.AP701", productName: "苹果", judgment: { ...deskReport.judgment, stance: "偏空" } } : deskReport,
    "/api/app/brief-subscriptions": async () => { await held; return { limit: 4, source: "user", overLimit: false, products: [{ product: "CZCE.AP", name: "苹果", symbol: "CZCE.AP701" }] }; },
  });
  const view = render(<FuturesDecisionWorkspace {...props({ runtimeBaseUrl: "https://subs-held.example.test",
    instruments: { ...instruments, items: [...instruments.items, apple] } }) as any} />);
  const judges = () => within(screen.getByRole("group", { name: "今日判断" })).getAllByRole("button").map(button => button.getAttribute("aria-label"));
  try {
    // The brief and the chart's desk read are back; the subscriptions are not: nothing says 暂无 or a code.
    await waitFor(() => expect(within(screen.getByTestId("mobile-feed")).getByText(/产区走货加快/)).toBeInTheDocument());
    await waitFor(() => expect(judges()).toContain("苹果 读取中"));
    expect(judges().join("|")).not.toMatch(/暂无|CZCE/);
    await act(async () => { release(); });
    await waitFor(() => expect(judges()).toEqual(["苹果 今日判断", "沪金 今日判断"]));
  } finally {
    view.unmount(); control.mockRestore();
  }
});

test("手机版我的：各子页进入与返回，mobilePage 命令与快照、持仓分页命令落到持仓与委托；电脑布局拒绝", () => {
  stubPhone(true);
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const view = render(<FuturesDecisionWorkspace {...props() as any} />);
  const state = () => control.mock.calls.at(-1)?.[2] as Record<string, unknown>;
  const apply = (changes: Record<string, unknown>) => act(() => { control.mock.calls.at(-1)?.[3](changes as any); });
  const help = vi.fn();
  window.addEventListener("ai-trader-help", help);
  try {
    fireEvent.click(tabButton("我的"));
    const page = screen.getByTestId("mobile-me");
    expect(page).toHaveTextContent("快期模拟账户 · 权益 1,026,580");
    expect(page).toHaveTextContent("回放和画线请在电脑上使用。");
    fireEvent.click(within(page).getByRole("button", { name: /^持仓与委托/ }));
    expect(screen.getByTestId("mobile-positions")).toBeVisible();
    expect(state()).toMatchObject({ mobileTab: "me", mobilePage: "positions" });
    fireEvent.click(screen.getByRole("button", { name: "收起持仓" }));
    expect(state()).toMatchObject({ mobilePage: null });
    fireEvent.click(within(page).getByRole("button", { name: /^复盘摘要/ }));
    expect(screen.getByTestId("mobile-review")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "返回我的" }));
    expect(screen.queryByTestId("mobile-review")).toBeNull();
    // AI 简报推送 (2026-10-05): its own page with the push switch and the subscribed products.
    fireEvent.click(within(page).getByRole("button", { name: /^AI 简报推送/ }));
    expect(screen.getByTestId("mobile-briefs")).toBeVisible();
    expect(within(screen.getByTestId("mobile-briefs")).getByRole("switch", { name: "AI 简报推送" })).toBeInTheDocument();
    expect(state()).toMatchObject({ mobilePage: "briefs" });
    fireEvent.click(screen.getByRole("button", { name: "返回我的" }));
    expect(screen.queryByTestId("mobile-briefs")).toBeNull();
    apply({ mobilePage: "briefs" });
    expect(screen.getByTestId("mobile-briefs")).toBeVisible();
    apply({ mobilePage: null });
    fireEvent.click(within(page).getByRole("button", { name: /^交易账户/ }));
    expect(screen.getByRole("dialog", { name: "设置" })).toBeVisible();
    expect(state()).toMatchObject({ settingsOpen: true, settingsPage: "account", mobilePage: "accounts" });
    apply({ mobilePage: "appearance" });
    expect(state()).toMatchObject({ settingsPage: "ui", mobilePage: "appearance" });
    apply({ mobilePage: "memory" });
    expect(state()).toMatchObject({ settingsPage: "memory", mobilePage: "memory" });
    apply({ mobilePage: null });
    expect(state()).toMatchObject({ settingsOpen: false, mobilePage: null });
    fireEvent.click(within(page).getByRole("button", { name: /^帮助/ }));
    expect((help.mock.calls[0][0] as CustomEvent).detail).toBe("mobile");
    // 持仓分页 / 下单 / 复盘 commands land on the phone's pages instead of desktop panels.
    apply({ positionsTab: "orders" });
    expect(state()).toMatchObject({ mobilePage: "positions", positionsTab: "orders" });
    apply({ mobilePage: "order" });
    expect(screen.getByTestId("mobile-order")).toBeVisible();
    apply({ mobilePage: "review" });
    expect(screen.queryByTestId("mobile-order")).toBeNull();
    expect(state()).toMatchObject({ mobilePage: "review" });
    apply({ mobileTab: "research" });
    expect(state()).toMatchObject({ mobileTab: "research", mobilePage: null });
  } finally {
    window.removeEventListener("ai-trader-help", help);
    view.unmount(); control.mockRestore();
  }
  stubPhone(false);
  const desktopControl = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const desktop = render(<FuturesDecisionWorkspace {...props() as any} />);
  try {
    expect(desktopControl.mock.calls.at(-1)?.[2]).not.toHaveProperty("mobilePage");
    expect(() => act(() => { desktopControl.mock.calls.at(-1)?.[3]({ mobilePage: "order" } as any); })).toThrow("电脑布局");
    expect(() => act(() => { desktopControl.mock.calls.at(-1)?.[3]({ mobileNotifyFilter: "fill" } as any); })).toThrow("电脑布局");
  } finally {
    desktop.unmount(); desktopControl.mockRestore();
  }
});


test("点顶栏权益打开账户明细：切到复盘（2026-10-22，本站模拟两本账样稿）", () => {
  render(<FuturesDecisionWorkspace {...props({}) as any} />);
  fireEvent.click(screen.getByRole("button", { name: /查看账户明细$/ }));
  expect(screen.getByRole("button", { name: "复盘" })).toHaveClass("active");
});

test("the replayed history's finished answers re-read the preferences once, not once per answer", async () => {
  preferencesRuntime();
  const reads = () => (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls
    .filter(([input, init]) => new URL(input instanceof Request ? input.url : String(input)).pathname === "/api/preferences"
      && (input instanceof Request ? input.method : init?.method ?? "GET") === "GET").length;
  const answers = (count: number) => Array.from({ length: count }, (_, index) => (
    { id: `answer-${index}`, role: "assistant", text: "好的", timestamp: "2026-10-20T01:00:00Z", completed: true, cards: [] }));
  const values = props({ runtimeBaseUrl: "https://runtime.example.test" });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  await waitFor(() => expect(reads()).toBe(1));
  for (let count = 1; count <= 30; count += 1) {
    view.rerender(<FuturesDecisionWorkspace {...values as any} state={{ ...values.state, turns: answers(count) }} />);
  }
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 1_700)); });
  expect(reads()).toBe(2);
  view.unmount();
});


test.each([
  ["CME.GCEZ26", "KQD.m@COMEX.GC"],
  ["CME.MGCZ26", undefined],
])("账户复盘币种与历史选择不依赖 %s 的实时行情", async (historySymbol, chartSymbol) => {
  const chart = { ...instruments.items[0], symbol: "KQD.m@COMEX.GC", strategyScope: "CME.GCE" };
  rememberStrategyScopes([chart]);
  const trade = { id: "usd-history", symbol: historySymbol, direction: "long", volume: 1,
    openTime: Date.parse("2026-10-21T09:00:00+08:00") * 1e6, closeTime: Date.parse("2026-10-21T10:00:00+08:00") * 1e6,
    openPrice: 4100, closePrice: 4101, gross: 10, fees: 5, net: 5, tradingDay: "2026-10-21", note: null };
  let note: { text: string; via: string; updatedAt: string } | null = null;
  const writes: { method: string; account: string | null }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    const id = new Headers(init?.headers).get("X-AI-Trader-Account");
    let body: unknown = {};
    if (url.includes("/trade-notes/")) {
      writes.push({ method: init?.method ?? "GET", account: id });
      note = init?.method === "DELETE" ? null : { text: JSON.parse(String(init?.body)).text, via: "typed", updatedAt: "2026-10-23T08:00:00Z" };
      body = { note, deleted: true };
    } else if (url.includes("/account/pnl")) body = { initialBalance: 100000, equity: 100005, totalPnl: 5, today: 0, thisMonth: 5,
      costSource: "foreign", dataStatus: "normal", skippedSymbols: [], trades: id === "paperusd:u1" ? [{ ...trade, note }] : [] };
    return new Response(JSON.stringify(body), { status: Object.keys(body as object).length ? 200 : 404 });
  }));
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const values = props({ runtimeBaseUrl: "https://review.example.test", instruments: { ...instruments, items: [...instruments.items, chart] },
    state: { turns: [], runtime: { simulationTrading: true, marketDataStatus: "normal", account: { id: "paper:u1", foreignAccountId: "paperusd:u1", role: "owner", executionOwner: true } } } });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  const state = () => control.mock.calls.at(-1)![2];
  const command = (changes: object) => act(() => { control.mock.calls.at(-1)![3](changes as any); });
  try {
    command({ view: "review" });
    await screen.findByRole("radio", { name: "美元" });
    await waitFor(() => expect(state().review).toMatchObject({ currency: "CNY", currencies: ["CNY", "USD"] }));
    // Readiness follows the actual registered handler, rather than only the page heading.
    await waitFor(() => expect(uiSurface("review")?.apply).toBeDefined());
    command({ reviewCurrency: "USD" });
    await screen.findByRole("button", { name: new RegExp(historySymbol + " · 多 1 手") });
    command({ reviewSelect: trade.id });
    expect(values.onSelectSymbol).toHaveBeenCalledTimes(chartSymbol ? 1 : 0);
    if (chartSymbol) expect(values.onSelectSymbol).toHaveBeenLastCalledWith(chartSymbol);
    expect(state().review).toMatchObject({ currency: "USD", selectedTradeId: trade.id, selectedNote: null });
    view.rerender(<FuturesDecisionWorkspace {...values as any} selectedSymbol={chartSymbol ?? values.selectedSymbol} market={undefined} marketLoading={false} marketError="当前行情暂时不可用" />);
    const journal = screen.getByRole("region", { name: "这笔交易" });
    expect(journal).toHaveTextContent(historySymbol);
    expect(screen.getByRole("button", { name: "重新读取" }).closest(".fdw-chart-area")).toBeTruthy();
    expect(document.querySelectorAll(".ai-orb")).toHaveLength(1);
    expect(screen.getByText(/仍可查看和补充记录/)).toBeVisible();
    fireEvent.change(within(journal).getByRole("textbox"), { target: { value: "美元历史补充" } });
    fireEvent.click(within(journal).getByRole("button", { name: "保存补充" }));
    await within(journal).findByText("已保存");
    expect(state().review).toMatchObject({ currency: "USD", selectedTradeId: trade.id, selectedNote: "美元历史补充" });
    fireEvent.change(within(journal).getByRole("textbox"), { target: { value: "" } });
    fireEvent.click(within(journal).getByRole("button", { name: "删除补充" }));
    await within(journal).findByText("已删除");
    expect(writes).toEqual([{ method: "PUT", account: "paperusd:u1" }, { method: "DELETE", account: "paperusd:u1" }]);
    command({ reviewCurrency: "CNY" });
    await waitFor(() => expect(state().review).toMatchObject({ currency: "CNY", selectedTradeId: null, selectedNote: null }));
    expect(screen.queryByRole("region", { name: "这笔交易" })).toBeNull();
  } finally { view.unmount(); control.mockRestore(); }
});

test("习惯对照 reaches the formal snapshot as review.habits (not only the review page's own registration)", async () => {
  const habits = { status: "ok", basis: "net", days: 90, currency: "CNY", rules: [], actual: { count: 9, pnl: 3670 },
    planned: { count: 6, pnl: 1995 }, gap: -1675, split: [{ tag: "adhoc", label: "临时", count: 3, pnl: 1675 }],
    holding: { lateLosers: null, earlyWinners: null },
    picks: [{ id: "h1", tradingDay: "2026-10-21", symbol: "SHFE.rb2701", direction: "long", volume: 1, tag: "adhoc", pnl: 900, reason: "计划外盈利" }] };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input instanceof Request ? input.url : input);
    const body = url.includes("/account/habits") ? habits : url.includes("/account/pnl") ? { initialBalance: 100000, equity: 100000,
      totalPnl: 0, today: 0, thisMonth: 0, costSource: "estimate", dataStatus: "normal", skippedSymbols: [], trades: [] } : {};
    return new Response(JSON.stringify(body), { status: Object.keys(body).length ? 200 : 404 });
  }));
  const control = vi.spyOn(uiControl, "useUiControl").mockImplementation(() => {});
  const values = props({ runtimeBaseUrl: "https://habits.example.test",
    state: { turns: [], runtime: { simulationTrading: true, marketDataStatus: "normal", account: { id: "paper:u1", role: "owner", executionOwner: true } } } });
  const view = render(<FuturesDecisionWorkspace {...values as any} />);
  try {
    act(() => { control.mock.calls.at(-1)![3]({ view: "review" } as any); });
    await screen.findByTestId("habit-compare");
    await waitFor(() => expect(control.mock.calls.at(-1)![2].review?.habits).toEqual(
      { status: "ok", actual: { count: 9, pnl: 3670 }, planned: { count: 6, pnl: 1995 }, gap: -1675, picks: ["h1"] }));
  } finally { view.unmount(); control.mockRestore(); }
});
