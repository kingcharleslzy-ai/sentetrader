import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { listInstruments, setFeedFocus, type InstrumentCatalog, type MarketWorkbenchSnapshot } from "../api";
import { setChartPeriod, strategyError } from "../strategyApi";
import { feedIsLive, type RuntimeFeedState } from "../runtimeFeed";
import type { WorkbenchDataStatus } from "./useWorkbenchStatus";
import type { WorkspaceHandle } from "./workspaceHandle";

import { registerFeedFocus } from "../feedView";
import { startupDuration } from "../userPreferences";
import { cachedMarket, feedFocusSince, loadCachedMarket, loadInstrumentNames, readMarketWorkbench, refuseFeedSince, rememberInstrumentNames, rememberMarket } from "../marketCache";
import { loginIdentity } from "../runtimeClient";
import { useIdentityFence } from "./useIdentityFence";
import { activeTradingAccount, onTradingAccountChange } from "../tradingAccountScope";
const SELECTED_INSTRUMENT_KEY = "ai-trader:selected-instrument";

interface MarketWorkbenchInput {
  handle: WorkspaceHandle;
  marketStatus: WorkbenchDataStatus;
  feed: RuntimeFeedState;
  replaySymbol?: string;
  chartTimeframe?: MarketWorkbenchSnapshot["durationSeconds"];
  onChartTimeframeChange?: (value: MarketWorkbenchSnapshot["durationSeconds"]) => void;
}

/**
 * Instrument catalog, the selected symbol/period and its market workbench.
 * The Runtime feed delivers the market while the stream is live; the poll
 * stands in only until the feed has produced this symbol and period.
 */
/** The periods a strategy can run on (the chart offers more: 30 分, 周线). */
const STRATEGY_TIMEFRAMES: ReadonlySet<number> = new Set([60, 180, 300, 900, 3600, 86400]);

export function useMarketWorkbench({ handle, marketStatus, feed, replaySymbol, chartTimeframe, onChartTimeframeChange }: MarketWorkbenchInput) {
  const { baseUrl, connection, sessionId, replayPath, setToast } = handle;
  const [catalogRead, setCatalogRead] = useState<{ runtime: string; catalog?: InstrumentCatalog }>();
  const instrumentCatalog = catalogRead?.runtime === baseUrl ? catalogRead.catalog : undefined;
  const setInstrumentCatalog = useCallback((catalog?: InstrumentCatalog) => setCatalogRead({ runtime: baseUrl, catalog }), [baseUrl]);
  const [nameCache, setNameCache] = useState<{ runtime: string; names: Record<string, string> }>();
  const cachedNames = nameCache?.runtime === baseUrl ? nameCache.names : undefined;
  useEffect(() => {
    let current = true;
    void loadInstrumentNames(baseUrl).then(names => {
      if (current) setNameCache(previous => previous?.runtime === baseUrl
        ? { runtime: baseUrl, names: { ...names, ...previous.names } } : { runtime: baseUrl, names });
    });
    return () => { current = false; };
  }, [baseUrl]);
  // Cached names travel to all display consumers, without making a cached contract selectable.
  const displayCatalog = useMemo<InstrumentCatalog | undefined>(() => {
    if (instrumentCatalog) return { ...instrumentCatalog, cachedNames };
    if (!cachedNames) return undefined;
    return { schemaVersion: 1, capturedAt: "", dataStatus: "stale", isCached: true,
      catalogStatus: "loading", count: 0, items: [], cachedNames };
  }, [instrumentCatalog, cachedNames]);
  const [instrumentCatalogLoading, setInstrumentCatalogLoading] = useState(false);
  const [instrumentCatalogError, setInstrumentCatalogError] = useState<string>();
  const [instrumentCatalogKey, setInstrumentCatalogKey] = useState(0);
  const [symbol, setSymbol] = useState<string | undefined>(() =>
    replaySymbol || (typeof window === "undefined" ? undefined : window.localStorage.getItem(SELECTED_INSTRUMENT_KEY) || undefined)
  );
  const [duration, setDuration] = useState<MarketWorkbenchSnapshot["durationSeconds"]>(chartTimeframe || startupDuration());
  const durationRef = useRef(duration);
  durationRef.current = duration;
  // A replay never shows or keeps a cached chart; live windows start from the last chart they saw.
  const cached = useCallback((forSymbol: string | undefined, forDuration: number) => replayPath ? undefined : cachedMarket(baseUrl, forSymbol, forDuration), [baseUrl, replayPath]);
  const [marketRead, setMarketRead] = useState(() => ({ baseUrl, market: cached(symbol, duration) }));
  // Runtime changes exclude the former snapshot during render, before effects or new reads.
  const market = marketRead.baseUrl === baseUrl ? marketRead.market : cached(symbol, duration);
  const setMarket = useCallback((snapshot: MarketWorkbenchSnapshot | undefined) => setMarketRead({ baseUrl, market: snapshot }), [baseUrl]);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketFailure, setMarketFailure] = useState<{ baseUrl: string; symbol?: string; duration: number; message: string }>();
  const marketError = marketFailure?.baseUrl === baseUrl && marketFailure.symbol === symbol && marketFailure.duration === duration
    ? marketFailure.message : undefined;
  const setMarketError = useCallback((message?: string) => setMarketFailure(message ? { baseUrl, symbol, duration, message } : undefined), [baseUrl, symbol, duration]);
  // Opening the page (or a chart this page has not held yet): the browser's stored chart, drawn only while
  // nothing for this target is on screen — a live read or push that came first is never replaced by it.
  useEffect(() => {
    if (replayPath || !symbol) return;
    let current = true;
    void loadCachedMarket(baseUrl, symbol, duration).then(snapshot => {
      if (!current || !snapshot) return;
      setMarketRead(read => read.baseUrl === baseUrl && read.market?.durationSeconds === duration
        && read.market.quote.symbol.toUpperCase() === symbol.toUpperCase() ? read : { baseUrl, market: snapshot });
    });
    return () => { current = false; };
  }, [baseUrl, symbol, duration, replayPath]);
  // Refreshes keep this target's failure until a result arrives; changing the target retires it.
  useEffect(() => { setMarketFailure(undefined); }, [baseUrl, symbol, duration]);
  const [marketKey, setMarketKey] = useState(0);
  // The market status matters to the catalog only in a replay; elsewhere its changes do not read the catalog again.
  const catalogNotReady = Boolean(replayPath) && marketStatus !== "normal";

  useEffect(() => {
    if (connection !== "connected" || catalogNotReady) {
      setInstrumentCatalog(undefined);
      setInstrumentCatalogLoading(false);
      return;
    }
    let cancelled = false;
    let catalogTimer: number | undefined;
    setInstrumentCatalogLoading(true);
    setInstrumentCatalogError(undefined);
    const loadCatalog = () => void listInstruments(baseUrl)
      .then((catalog) => {
        if (cancelled) return;
        setInstrumentCatalog(catalog);
        void rememberInstrumentNames(baseUrl, catalog).then(names => {
          if (!cancelled) setNameCache({ runtime: baseUrl, names });
        });
        if (catalog.catalogStatus === "loading") catalogTimer = window.setTimeout(loadCatalog, 1_000);
      })
      .catch(() => { if (!cancelled) setInstrumentCatalogError("合约目录暂时不可用"); })
      .finally(() => { if (!cancelled) setInstrumentCatalogLoading(false); });
    loadCatalog();
    return () => {
      cancelled = true;
      if (catalogTimer !== undefined) window.clearTimeout(catalogTimer);
    };
  }, [baseUrl, connection, instrumentCatalogKey, catalogNotReady, setInstrumentCatalog]);

  useEffect(() => {
    if (!instrumentCatalog?.items.length) return;
    if (symbol && instrumentCatalog.catalogStatus === "loading") return;
    // A remembered symbol in another case (DCE.i2609 vs DCE.I2609) is normalised to the catalog's spelling.
    const listed = symbol && instrumentCatalog.items.find((item) => item.symbol.toUpperCase() === symbol.toUpperCase());
    if (listed) { if (listed.symbol !== symbol) setSymbol(listed.symbol); return; }
    const initial = instrumentCatalog.items.find((item) => item.isMain) ?? instrumentCatalog.items[0];
    setSymbol(initial.symbol);
  }, [instrumentCatalog, symbol]);

  // Only the live window remembers its contract: a replay's (often expired) contract must not
  // become the live chart's contract after the replay ends.
  useEffect(() => {
    if (!symbol || replayPath || typeof window === "undefined") return;
    window.localStorage.setItem(SELECTED_INSTRUMENT_KEY, symbol);
  }, [symbol, replayPath]);

  const feedActive = connection === "connected" && !replayPath;
  // The provider reports symbols in exchange case (DCE.I2609) while the selection keeps catalog case.
  const feedMarketMatches = feed.market !== undefined && feed.market.durationSeconds === duration
    && feed.market.quote.symbol.toUpperCase() === symbol?.toUpperCase();
  const feedCovers = useRef(false);
  // Only the market channel answers for the market: an execution-control push a second
  // ago says nothing about a quote that stopped updating a minute ago.
  feedCovers.current = feedActive && feedIsLive(feed, "market") && feedMarketMatches;
  useEffect(() => {
    if (!feedActive || !feed.market || !feedMarketMatches) return;
    setMarket(feed.market);
    setMarketError(undefined);
  }, [feed.market, feedActive, feedMarketMatches, setMarket, setMarketError]);
  useEffect(() => {
    if (!feedActive || !sessionId || !symbol) return;
    return registerFeedFocus(baseUrl, sessionId, async current => {
      // Re-evaluate held bars on each retry/reconnect, never sending a retired chart/session.
      const since = await feedFocusSince(baseUrl, sessionId, symbol, duration);
      if (!current()) return;
      try {
        await setFeedFocus(baseUrl, sessionId, symbol, duration, since);
      } catch (error) {
        if (current() && since === undefined) refuseFeedSince(baseUrl, symbol, duration);
        throw error;
      }
    });
  }, [baseUrl, feedActive, sessionId, symbol, duration]);

  useEffect(() => {
    if (connection !== "connected" || !symbol || (replayPath && marketStatus !== "normal")) {
      setMarket(cached(symbol, duration));
      setMarketLoading(false);
      return;
    }
    let cancelled = false;
    let inFlight = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    const load = async () => {
      if (cancelled || inFlight) return;
      if (document.visibilityState === "hidden" || feedCovers.current) {
        timer = window.setTimeout(() => void load(), 2_500);
        return;
      }
      inFlight = true;
      controller = new AbortController();
      setMarketLoading(true);
      try {
        const snapshot = await readMarketWorkbench(baseUrl, symbol, duration, controller.signal);
        if (!cancelled) {
          setMarket(snapshot);
          setMarketError(undefined);
        }
      } catch (error) {
        if (!cancelled && !(error instanceof DOMException && error.name === "AbortError")) {
          setMarketError(marketErrorMessage(error, Boolean(replayPath)));
        }
      } finally {
        inFlight = false;
        if (!cancelled) {
          setMarketLoading(false);
          timer = window.setTimeout(() => void load(), replayPath ? 250 : 2_500);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
      controller?.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [baseUrl, connection, duration, marketKey, symbol, marketStatus, cached, replayPath, setMarket, setMarketError]);

  const fence = useIdentityFence([baseUrl, connection, sessionId]);
  const accountRevision = useRef(0);
  useEffect(() => onTradingAccountChange(() => { accountRevision.current += 1; }), []);
  // A new UI fence cannot cancel sent writes. Each login × Runtime × account keeps its tail
  // through reconnects and A→B→A; unrelated accounts/Runtimes do not wait for that request.
  const chartPeriodTails = useRef(new Map<string, Promise<void>>());
  const chartPeriodQueue = useMemo(() => ({ fence,
    target: undefined as { symbol: string; duration: number; account: string | null; accountRevision: number } | undefined }), [fence]);
  const chartContext = useRef({ baseUrl, symbol });
  chartContext.current = { baseUrl, symbol };
  const changeChartPeriod = (next: MarketWorkbenchSnapshot["durationSeconds"], notify = true) => {
    // A symbol selection updates this target synchronously: the phone's multi-chart click and an
    // AI command can select symbol + period together before React renders the new symbol.
    const current = chartContext.current.symbol;
    if (!current || connection !== "connected" || !fence.isCurrent()) {
      if (notify && fence.isCurrent()) setToast("连接行情后可切换图表周期", "warning");
      return;
    }
    // Viewing never waits for a previous strategy response. Only the actual strategy writes queue.
    if (next !== durationRef.current) {
      durationRef.current = next;
      setMarket(cached(current, next));
      setDuration(next);
      onChartTimeframeChange?.(next);
    }
    const account = activeTradingAccount();
    // Rendering the new symbol also synchronizes its strategies; a combined pick already queued
    // that exact target. An explicit period pick still retries a previous refusal.
    if (!notify && chartPeriodQueue.target?.symbol === current && chartPeriodQueue.target.duration === next
        && chartPeriodQueue.target.account === account && chartPeriodQueue.target.accountRevision === accountRevision.current) return;
    const target = { symbol: current, duration: next, account, accountRevision: accountRevision.current };
    chartPeriodQueue.target = target;
    // Writes retain submission order. Only notices for the latest selected target may surface;
    // an account transition retires old work even if that same account is selected again.
    const writeIsCurrent = () => chartPeriodQueue.fence.isCurrent() && chartContext.current.symbol === current
      && activeTradingAccount() === account && accountRevision.current === target.accountRevision;
    const targetIsCurrent = () => writeIsCurrent() && chartPeriodQueue.target === target && durationRef.current === next;
    const login = loginIdentity();
    const owner = JSON.stringify([login, baseUrl, account]);
    // Without a status account id the header is omitted and the server resolves a default.
    // It may be any granted account, so unknown and known tails wait each other in this Runtime.
    const preceding = [...chartPeriodTails.current].filter(([key]) => {
      const [previousLogin, runtime, previousAccount] = JSON.parse(key) as [number, string, string | null];
      return previousLogin === login && runtime === baseUrl
        && (account === null || previousAccount === null || previousAccount === account);
    }).map(([, tail]) => tail);
    const run = Promise.all(preceding).then(async () => {
      if (!writeIsCurrent()) return;
      // Strategies run on 1/3/5/15 分、1 小时、日线 only: a 30 分 or 周线 view leaves the strategies
      // that follow the chart on their last period instead of asking for one they cannot run.
      if (!STRATEGY_TIMEFRAMES.has(next)) return;
      try {
        await setChartPeriod(baseUrl, current, next);
        if (!targetIsCurrent()) return;
        window.dispatchEvent(new Event("ai-trader-strategies-changed"));
      } catch (error) {
        if (!targetIsCurrent()) return;
        const reason = strategyError(error, "请稍后重试。").replace(/^周期尚未切换：/, "");
        setToast(`图表已切换；跟随图表周期的策略暂未切换：${reason}`, "warning");
      }
    });
    const tail = run.catch(() => undefined);
    chartPeriodTails.current.set(owner, tail);
    void tail.then(() => { if (chartPeriodTails.current.get(owner) === tail) chartPeriodTails.current.delete(owner); });
    return run;
  };
  useEffect(() => {
    void changeChartPeriod(durationRef.current, false);
  }, [baseUrl, connection, sessionId, symbol]);

  const selectSymbol = (next: string) => {
    // Selection entries pass whatever they display; the catalog spelling is canonical.
    const canonical = instrumentCatalog?.items.find((item) => item.symbol.toUpperCase() === next.toUpperCase())?.symbol ?? next;
    if (canonical !== symbol) setMarket(cached(canonical, durationRef.current));
    chartContext.current = { baseUrl, symbol: canonical };
    setSymbol(canonical);
  };

  useEffect(() => { if (!replayPath) rememberMarket(baseUrl, market); }, [baseUrl, market, replayPath]);

  return {
    instrumentCatalog: displayCatalog, instrumentCatalogLoading, instrumentCatalogError,
    retryInstruments: () => setInstrumentCatalogKey((value) => value + 1),
    symbol, duration, market, marketLoading, marketError,
    selectSymbol, changeChartPeriod,
    refreshMarket: () => setMarketKey((value) => value + 1)
  };
}

/** The Runtime's 503 detail names the contract's own condition when there is one; anything else is "unavailable". */
export const MARKET_ERROR_MESSAGES: Record<string, string> = {
  market_series_empty: "该合约上市以来没有成交，暂无行情",
  market_series_not_ready: "该合约行情正在订阅，稍后自动重试",
  market_quote_not_ready: "该合约暂无报价，等待下次行情更新",
  market_subscription_limit: "订阅数已满，稍后重试",
  sidecar_reconnecting: "行情重连中，稍后自动恢复"
};

export function marketErrorMessage(error: unknown, replay = false): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  const code = typeof detail === "string" ? detail : typeof detail === "object" && detail && typeof (detail as { detail?: unknown }).detail === "string" ? (detail as { detail: string }).detail : undefined;
  // A replay period still backfilling is loading, not a data failure; the 250 ms
  // poller swaps the chart in the moment the sidecar finishes that period.
  if (replay && code === "market_series_not_ready") return "该周期历史数据加载中…";
  return (code && MARKET_ERROR_MESSAGES[code]) || "当前行情暂时不可用";
}
