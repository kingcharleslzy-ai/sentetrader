import { useEffect, useRef, useState } from "react";
import type { InstrumentCatalog, MarketWorkbenchSnapshot } from "../api";
import { guestRequest, guestHeaders, guestAccessTokenFor, setGuestAccessToken, startGuestSession, type GuestChanges, type GuestSession } from "../guestApi";
import { mergeGuestMarket, type GuestMarketFrame } from "../guestMarket";
import { openSseStream } from "../sse";
import { rememberStartupDuration, startupDuration, type DurationSeconds } from "../userPreferences";
import { pushToTalkStore } from "../pushToTalk";

const SYMBOL_KEY = "ai-trader:selected-instrument";

/** Guest identity/transport only. Rendering and presentation are the normal workspace. */
export function useGuestAccount(baseUrl: string, initialSession: GuestSession) {
  const [session, setSession] = useState(initialSession);
  const [catalog, setCatalog] = useState<InstrumentCatalog>();
  const [symbol, setSymbol] = useState(() => localStorage.getItem(SYMBOL_KEY) || "");
  const [duration, setDuration] = useState<DurationSeconds>(startupDuration);
  const [reportedRevision, setReportedRevision] = useState(-1);
  const [market, setMarket] = useState<MarketWorkbenchSnapshot>();
  const [loginOpen, setLoginOpen] = useState(false);
  const [status, setStatus] = useState("正在连接行情…");
  const [notice, setNotice] = useState("");
  const [retry, setRetry] = useState(0);
  // The public catalog read itself: in flight (a retry shows loading and is not started twice) and its last failure.
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState<string>();
  const [expired, setExpired] = useState(false);
  const mounted = useRef(true);
  const appliedRevision = useRef(-1);
  const commands = useRef(Promise.resolve());
  const applyChanges = useRef<(changes: GuestChanges) => void | Promise<void>>(() => {});
  useEffect(() => {
    setGuestAccessToken(baseUrl, session.accessToken);
    appliedRevision.current = -1;
    setReportedRevision(-1);
    commands.current = Promise.resolve();
    return () => { if (guestAccessTokenFor(baseUrl) === session.accessToken) setGuestAccessToken(baseUrl); };
  }, [baseUrl, session]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; pushToTalkStore.cancel(); };
  }, []);
  useEffect(() => { if (loginOpen) pushToTalkStore.cancel(); }, [loginOpen]);
  useEffect(() => { if (symbol) localStorage.setItem(SYMBOL_KEY, symbol); rememberStartupDuration(duration); }, [symbol, duration]);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      setCatalogLoading(true);
      try {
        const value = await guestRequest<InstrumentCatalog>(baseUrl, "/api/guest/instruments");
        if (stopped) return;
        setCatalog(value);
        setCatalogError(undefined);
        setSymbol(current => value.items.find(item => item.symbol.toLowerCase() === current.toLowerCase())?.symbol || value.items.find(item => item.isMain)?.symbol || value.items[0]?.symbol || "");
        if (value.catalogStatus === "loading") timer = setTimeout(() => void load(), 2000);
        else if (!value.items.length) setStatus("公共行情暂不可用，请重试");
      } catch (error) {
        if (!stopped) { setStatus((error as Error).message); setCatalogError((error as Error).message); setExpired((error as {status?: number}).status === 401); }
      } finally { if (!stopped) setCatalogLoading(false); }
    };
    void load();
    return () => { stopped = true; clearTimeout(timer); };
  }, [baseUrl, session, retry]);

  useEffect(() => {
    if (!symbol || expired) return;
    let stopped = false;
    setMarket(undefined);
    setStatus("正在连接行情…");
    const query = `?symbol=${encodeURIComponent(symbol)}&duration=${duration}`;
    const accept = (frame: GuestMarketFrame, full: boolean) => {
      if (stopped) return;
      setMarket(previous => mergeGuestMarket(previous, frame, full));
      setStatus("");
    };
    const stop = openSseStream({ url: `${baseUrl}/api/guest/stream${query}`, retryMs: 3000,
      onFrame: frame => {
        if (stopped || !["snapshot", "update", "ui"].includes(frame.event)) return;
        try {
          const data = JSON.parse(frame.data.join("\n"));
          if (frame.event === "ui") {
            if (data.windowId !== session.windowId || data.revision <= appliedRevision.current) return;
            commands.current = commands.current.then(async () => {
              if (stopped || data.revision <= appliedRevision.current) return;
              await applyChanges.current(data.changes);
              if (stopped) return;
              appliedRevision.current = data.revision;
              setReportedRevision(data.revision);
            }).catch(() => { if (!stopped) setNotice("界面操作未完成，请重试"); });
          } else accept(data, frame.event === "snapshot");
        } catch { setStatus("行情数据暂不可用"); }
      },
      onClose: () => { if (!stopped) setStatus("行情连接中断，正在重连…"); },
      fetchImpl: async (input, init) => {
        const response = await fetch(input, { ...init, headers: guestHeaders(baseUrl, init?.headers) });
        if (response.status === 401 && !stopped) { setExpired(true); setStatus("游客会话已失效，请重新连接"); }
        return response;
      }
    });
    return () => { stopped = true; stop(); };
  }, [baseUrl, symbol, duration, retry, session, expired]);

  /** 合约目录“重试”: read the public catalog (and stream) again on the same session — no new guest session. */
  const reloadCatalog = () => {
    if (catalogLoading || expired) return;   // an expired session needs 重新连接, not a catalog re-read
    setCatalogLoading(true); setCatalogError(undefined); setStatus(""); setRetry(value => value + 1);
  };
  const reconnect = async () => {
    try { const next = await startGuestSession(baseUrl, () => mounted.current); if (next) { setSession(next); setExpired(false); setRetry(value => value + 1); } }
    catch (error) { if (mounted.current) setStatus((error as Error).message); }
  };
  const voice = async (text: string) => {
    try {
      const result = await guestRequest<{ accepted: boolean; changes?: GuestChanges; say: string }>(baseUrl, "/api/guest/voice/intent", { method: "POST", body: JSON.stringify({ text, windowId: session.windowId }) });
      if (!mounted.current) return;
      if (result.accepted && result.changes) await applyChanges.current(result.changes);
      setNotice(result.say);
    } catch (error) { if (mounted.current) setNotice((error as Error).message); }
  };
  const reportExpired = () => { setExpired(true); setStatus("游客会话已失效，请重新连接"); };
  return { baseUrl, session, catalog, symbol, setSymbol, duration, setDuration, market, loginOpen, setLoginOpen,
    status, notice, setNotice, expired, reportExpired, reportedRevision, applyChanges, reconnect, reloadCatalog, catalogLoading, catalogError, voice };
}

export type GuestAccountController = ReturnType<typeof useGuestAccount>;

/** What the guest workspace passes as `instrumentsLoading`: a catalog read in flight, or (without a failed
 *  read) no catalog yet / the Runtime's cold-start catalog still preparing. A failed read is the error. */
export function guestCatalogLoading(guest: Pick<GuestAccountController, "catalog" | "catalogLoading" | "catalogError">): boolean {
  return guest.catalogLoading || (!guest.catalogError && (!guest.catalog || guest.catalog.catalogStatus === "loading"));
}
