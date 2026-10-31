import { createContext, useCallback, useEffect, useState, type ReactNode } from "react";
import type { RuntimeMode } from "@ai-trader/ui-events";
import { getOwnerAuthStatus, setOwnerSessionToken } from "../api";
import { resolveRuntimeConnection, cloudRuntimeAuthMode } from "../runtimeConnection";
import { guestHeaders, startGuestSession, type GuestSession } from "../guestApi";
import { setRumTarget } from "../rum";
import { isRequestTimeout } from "../requestTimeout";
import { GuestAppearanceContext, type GuestAppearance } from "../guestAppearance";
import { FuturesDecisionWorkspace } from "./FuturesDecisionWorkspace";
import { AiAvatar } from "./AiAvatar";
import { OwnerLogin } from "./OwnerLogin";
import { guestCatalogLoading, useGuestAccount } from "../hooks/useGuestAccount";
import { useAppearance } from "../hooks/useAppearance";
import { useDesktopUpdater } from "../hooks/useDesktopUpdater";
import { isTauriRuntime } from "../runtimeConnection";
import { initialConversationState } from "../state/conversation";
import { readChartSettings } from "./futures/support";
import { pushToTalkStore } from "../pushToTalk";

export const ReturnToGuestContext = createContext<(() => void) | undefined>(undefined);
/** How long 正在连接行情… waits before it offers 刷新重试. */
const SLOW_CONNECT_MS = 10_000;

/** Decide identity before mounting any private workspace hook or replay service. */
export function GuestEntry({ mode, children }: { mode: RuntimeMode; children: ReactNode }) {
  const eligible = mode === "cloud" && cloudRuntimeAuthMode() === "owner-password";
  const [entry, setEntry] = useState<"checking" | "member" | "guest">(eligible ? "checking" : "member");
  const [identityRevision, setIdentityRevision] = useState(0);
  const returnToGuest = useCallback(() => { setStalled(undefined); setEntry("checking"); setIdentityRevision(value => value + 1); }, []);
  const [memberAppearance, setMemberAppearance] = useState<GuestAppearance>();
  const [guest, setGuest] = useState<{ baseUrl: string; session: GuestSession }>();
  // The identity read timed out (#710 B): stay on this page with the reason and a reload, rather than
  // dropping into the member login as if the Runtime had answered.
  const [stalled, setStalled] = useState<string>();
  useEffect(() => {
    if (!eligible) return;
    let stopped = false;
    void (async () => {
      try {
        const connection = await resolveRuntimeConnection(mode);
        if (stopped) return;
        if (connection.source === "tauri-ipc") setOwnerSessionToken(connection.baseUrl, connection.token);
        const owner = await getOwnerAuthStatus(connection.baseUrl, { maxAgeMs: 5000 });
        if (stopped) return;
        if (owner.authenticated) { setEntry("member"); return; }
        const session = await startGuestSession(connection.baseUrl, () => !stopped);
        if (session) { setGuest({ baseUrl: connection.baseUrl, session }); setEntry("guest"); }
      } catch (error) {
        if (stopped) return;
        if (isRequestTimeout(error)) { setStalled((error as Error).message); return; }
        // Older runtimes / disabled guest access retain their existing required-login flow.
        setEntry("member");
      }
    })();
    return () => { stopped = true; };
  }, [eligible, mode, identityRevision]);
  // A slow or blocked network leaves the identity check pending: after a while say so and offer a reload
  // (one fresh start instead of a second check racing the first one's guest session).
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    setSlow(false);
    if (entry !== "checking") return;
    const timer = window.setTimeout(() => setSlow(true), SLOW_CONNECT_MS);
    return () => window.clearTimeout(timer);
  }, [entry, identityRevision]);
  if (entry === "checking") return <main className="owner-login-gate app-boot" role="status"><AiAvatar kind="kline" state="busy" size={56} /><span>正在连接行情…</span>
    {stalled || slow ? <p className="app-boot-slow">{stalled ?? "仍在连接，网络较慢或应用服务暂时连不上"}<button type="button" className="ghost-button" onClick={() => window.location.reload()}>刷新重试</button></p> : null}</main>;
  if (entry === "guest" && guest) return <GuestAccount {...guest} onAuthenticated={appearance => { setMemberAppearance(appearance); setEntry("member"); }} />;
  return <ReturnToGuestContext.Provider value={eligible ? returnToGuest : undefined}><GuestAppearanceContext.Provider value={memberAppearance && { ...memberAppearance, consume: () => setMemberAppearance(undefined) }}>{children}</GuestAppearanceContext.Provider></ReturnToGuestContext.Provider>;
}


/** Supplies a read-only identity to the same workspace, without private account hooks. */
function GuestAccount({ baseUrl, session, onAuthenticated }: {
  baseUrl: string; session: GuestSession; onAuthenticated: (appearance: GuestAppearance) => void;
}) {
  const guest = useGuestAccount(baseUrl, session);
  useEffect(() => {
    setRumTarget({ baseUrl, headers: () => guestHeaders(baseUrl), identity: "guest" });
    return () => setRumTarget(undefined);
  }, [baseUrl, session]);
  const appearance = useAppearance(message => guest.setNotice(message ?? ""));
  const updater = useDesktopUpdater(isTauriRuntime());
  const login = () => guest.setLoginOpen(true);
  const blocked = async () => { login(); return false; };
  const blockedOrder = async () => { login(); return undefined; };
  const unavailable = async () => { login(); throw new Error("登录后可使用此功能"); };
  const connected = Boolean(guest.market) && !guest.expired && !guest.status;
  return <>
    <FuturesDecisionWorkspace guest={guest} mode="cloud" runtimeBaseUrl={baseUrl}
      connection={connected ? "connected" : guest.expired ? "disconnected" : "connecting"}
      state={initialConversationState}
      status={{ now: Date.now(), runtimeLive: connected, runtimeFresh: connected, runtimeUsable: connected,
        agentStatus: "unknown", accountStatus: "unknown", marketStatus: guest.market?.dataStatus ?? "unknown", riskStatus: "unknown" }}
      instruments={guest.catalog} instrumentsLoading={guestCatalogLoading(guest)} instrumentsError={guest.catalogError}
      market={guest.market} marketLoading={!guest.market && !guest.expired} duration={guest.duration} selectedSymbol={guest.symbol}
      simulationOrders={[]} aiExecutionBusy={false} simulationGatewayAvailable={false} simulationEnabled={false}
      simulationBlockedReason="登录后可使用模拟交易" tradeBusy={false}
      fontScale={appearance.fontScale} interfaceDensity={appearance.interfaceDensity}
      onFontScaleChange={appearance.setFontScale} onInterfaceDensityChange={appearance.setInterfaceDensity}
      simulationLimits={{ enabled: false, maxOrderVolume: 0, maxOrders: 0 }} onSimulationLimitsChange={login}
      proposalActions={{ adjustRisk: login, confirm: login, reject: login, cancelRun: login,
        changeStrategyMonitoring: unavailable, loadStrategyMonitor: unavailable }} updater={updater}
      onSelectSymbol={symbol => { if (guest.catalog?.items.some(item => item.symbol === symbol)) guest.setSymbol(symbol); else { guest.setNotice("游客只能看螺纹主力合约，登录后可看其他品种"); login(); } }}
      onDurationChange={guest.setDuration}
      onRefreshMarket={() => void guest.reconnect()} onRetryInstruments={guest.reloadCatalog} onRefreshAccount={login}
      onManualSubmit={blockedOrder} onTargetPosition={blocked} onOrderAction={login} onStopAiExecution={login}
      onPrompt={async () => login()} onOpenCloudLogin={login} onOpenReplay={login} />
    <OwnerLogin baseUrl={baseUrl} open={guest.loginOpen} required={false} onClose={() => guest.setLoginOpen(false)}
      onAuthenticated={notice => { pushToTalkStore.cancel(); onAuthenticated({ theme: readChartSettings().theme, duration: guest.duration, notice }); }} />
  </>;
}
