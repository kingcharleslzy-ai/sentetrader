import { useEffect, useReducer, useState, useRef } from "react";
import type { BacktestComparison, RuntimeMode } from "@ai-trader/ui-events";
import { applyFeedEvent, resetFeed, markFeedStreamAlive } from "../runtimeFeed";
import { resendFeedSubscriptions, setFeedViewTarget } from "../feedView";
import { authenticateCloud, authenticateLocal, setOwnerSessionToken, getOwnerAuthStatus, openEventStream, resolveRuntimeConnection, runtimeUrl, sendMessage, type FuturesWorkspacePromptContext, type MessageExtras } from "../api";
import { requestJson } from "../api";
import { setCloudAccessToken } from "../runtimeConnection";
import { isRequestTimeout, isTransientFailure } from "../requestTimeout";
import {
  conversationReducer,
  initialConversationState
} from "../state/conversation";
import { useProposalSettlement } from "./useProposalSettlement";

export type ConnectionState = "connecting" | "connected" | "disconnected";

export function useRuntimeSession(mode: RuntimeMode, reconnectKey = 0, replayPath = "") {
  const [state, dispatch] = useReducer(conversationReducer, initialConversationState);
  const [sessionId, setSessionId] = useState<string>();
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [runtimeActivityAt, setRuntimeActivityAt] = useState<number>();
  const [baseUrl, setBaseUrl] = useState(() => runtimeUrl(mode));
  const [rootBaseUrl, setRootBaseUrl] = useState(() => runtimeUrl(mode));
  const [networkReconnectKey, setNetworkReconnectKey] = useState(0);
  const [workspaceScope, setWorkspaceScope] = useState<string>();
  const [rootWorkspaceScope, setRootWorkspaceScope] = useState<string>();
  const [authRequired, setAuthRequired] = useState(false);
  const [chatBusy, setChatBusy] = useState(false);
  const stream = useRef<() => void>(undefined);
  const streamEpoch = useRef(0);
  const retireFeedTarget = useRef<() => void>(undefined);
  const chatOperation = useRef(false);
  const currentScope = useRef(workspaceScope);
  currentScope.current = workspaceScope;
  // The stream client reconnects on its own within a second; a drop that short
  // stays invisible instead of clearing charts and positions for a blink.
  const pendingDisconnect = useRef<{epoch: number; timer: number} | undefined>(undefined);
  const cancelPendingDisconnect = () => {
    const pending = pendingDisconnect.current;
    pendingDisconnect.current = undefined;
    if (pending) window.clearTimeout(pending.timer);
  };
  const attachSession = (url: string, id: string) => {
    stream.current?.();
    cancelPendingDisconnect();
    const epoch = ++streamEpoch.current;
    dispatch({type: "reset"}); setSessionId(id);
    resetFeed(id);
    retireFeedTarget.current?.();
    retireFeedTarget.current = setFeedViewTarget(url, id);
    stream.current = openEventStream(url, id,
      event => { if (epoch !== streamEpoch.current) return; if (!applyFeedEvent(id, event)) dispatch({type: "event", event}); },
      message => { if (epoch === streamEpoch.current) dispatch({type: "local-warning", message}); },
      () => { if (epoch === streamEpoch.current) { markFeedStreamAlive(id); setRuntimeActivityAt(Date.now()); } },
      connected => {
        if (epoch !== streamEpoch.current) return;
        if (connected) {
          // Every (re)opened stream hears what this window shows (a restarted Runtime knows nothing).
          resendFeedSubscriptions(url, id);
          cancelPendingDisconnect();
          setConnection("connected");
          dispatch({type: "connection-restored"});
          return;
        }
        if (pendingDisconnect.current) return;
        const timer = window.setTimeout(() => {
          pendingDisconnect.current = undefined;
          if (epoch === streamEpoch.current) setConnection("disconnected");
        }, 3000);
        pendingDisconnect.current = {epoch, timer};
      });
  };
  const chatKey = (scope: string) => `ai-trader-chat-current:${scope}`;
  const saveChat = (scope: string, id: string) => { try { localStorage.setItem(chatKey(scope), id); } catch { /* Server history remains available. */ } };
  const changeChat = async (id?: string) => {
    if (!workspaceScope || !sessionId || chatOperation.current) return;
    const scope = workspaceScope, epoch = streamEpoch.current;
    chatOperation.current = true; setChatBusy(true);
    try {
      const next = id
        ? await requestJson<{sessionId: string}>(`${baseUrl}/api/sessions/${encodeURIComponent(id)}`)
        : await requestJson<{sessionId: string}>(`${baseUrl}/api/sessions?chat=true`, {method: "POST", body: JSON.stringify({mode: replayPath ? "local" : mode, agentId: "native"})});
      if (scope !== currentScope.current || epoch !== streamEpoch.current) return;
      saveChat(scope, next.sessionId); attachSession(baseUrl, next.sessionId);
    } finally { chatOperation.current = false; setChatBusy(false); }
  };
  const listChats = () => requestJson<{items: {sessionId: string; createdAt: string}[]}>(`${baseUrl}/api/chat-sessions?mode=${replayPath ? "local" : mode}`);


  useEffect(() => {
    const offline = () => setConnection("disconnected");
    const online = () => {
      setConnection("connecting");
      setNetworkReconnectKey((value) => value + 1);
    };
    window.addEventListener("offline", offline);
    window.addEventListener("online", online);
    return () => {
      window.removeEventListener("offline", offline);
      window.removeEventListener("online", online);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    ++streamEpoch.current; stream.current?.(); retireFeedTarget.current?.();
    let retryTimer: number | undefined;
    let retryCount = 0;
    dispatch({ type: "reset" });
    setSessionId(undefined);
    setWorkspaceScope(undefined);
    setRootWorkspaceScope(undefined);
    setConnection("connecting");

    // Unmounted (or superseded) mid-attempt: stop before the next request instead of only ignoring its answer.
    const live = () => { if (cancelled) throw new Error("connection_cancelled"); };
    const connect = async () => {
      const runtime = await resolveRuntimeConnection(mode);
      live();
      setRootBaseUrl(runtime.baseUrl);
      if (runtime.authMode === "cloud-jwt") {
        await authenticateCloud(runtime.baseUrl, runtime.token);
        setCloudAccessToken(undefined);
      } else if (runtime.authMode === "owner-password") {
        if (runtime.source === "tauri-ipc") setOwnerSessionToken(runtime.baseUrl, runtime.token);
        const owner = await getOwnerAuthStatus(runtime.baseUrl, { maxAgeMs: 5000 });
        if (!owner.authenticated) throw new Error("owner_auth_required");
      } else {
        await authenticateLocal(runtime.baseUrl, runtime.token);
      }
      live();
      const workspace = await requestJson<{scope: string}>(`${runtime.baseUrl}/api/workspace`);
      live();
      setRootWorkspaceScope(`${workspace.scope}:${mode}`);
      setWorkspaceScope(`${workspace.scope}:${mode}${replayPath}`);
      runtime.baseUrl += replayPath;
      setBaseUrl(runtime.baseUrl);
      // A server-owned user session survives ephemeral desktop ports and avoids
      // discarding history when a transient resume request fails.
      const scope = `${workspace.scope}:${mode}${replayPath}`;
      let savedId: string | null = null;
      try { savedId = localStorage.getItem(chatKey(scope)); } catch { /* Restore server history. */ }
      let restored: {sessionId: string} | undefined;
      if (savedId) {
        try { restored = await requestJson<{sessionId: string}>(`${runtime.baseUrl}/api/sessions/${encodeURIComponent(savedId)}`); }
        catch (error) { if ((error as {status?: number}).status !== 404) throw error; }
        live();
      }
      restored ??= await requestJson<{sessionId: string}>(`${runtime.baseUrl}/api/sessions/restore`, {
        method: "POST", body: JSON.stringify({ mode: replayPath ? "local" : mode, agentId: "native" })
      });
      if (!cancelled) saveChat(scope, restored.sessionId);
      const nextSessionId = restored.sessionId;

      return { nextSessionId, runtimeBaseUrl: runtime.baseUrl };
    };

    const attempt = () => void connect()
      .then(({ nextSessionId, runtimeBaseUrl }) => {
        if (cancelled) return;
        setAuthRequired(false);
        attachSession(runtimeBaseUrl, nextSessionId);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : String(error || "");
        const status = typeof error === "object" && error ? (error as {status?: number}).status : undefined;
        const ownerLoginRequired = message.includes("owner_auth_required") || (mode === "cloud" && status === 401);
        setAuthRequired(ownerLoginRequired);
        setConnection("disconnected");
        if (ownerLoginRequired) return;
        // The shared classification (#710 P2): a transport failure, a 5xx or a read that ran past its
        // limit retries with backoff — the service answering again reconnects on its own. The writes in
        // this flow (auth with the launch token, session restore) are safe to repeat; a timed-out write
        // never counts as transient. Attempts run one after another, never overlapping.
        const transient = isTransientFailure(error);
        // Keep transport diagnostics separate from user-facing text; never log
        // the authenticated request or a server response body here.
        console.warn("runtime_connection_failed", {status, name: error instanceof Error ? error.name : "Error",
          cause: error instanceof TypeError ? message : isRequestTimeout(error) ? "request_timeout" : undefined});
        dispatch({type: "local-error", message: transient
          ? isRequestTimeout(error) ? "应用服务暂时没有响应，正在重试连接" : "应用服务暂时不可用，正在重试连接"
          : "无法连接应用服务，请检查连接设置"});
        if (transient) retryTimer = window.setTimeout(attempt, Math.min(1_000 * 2 ** retryCount++, 15_000));
      });
    attempt();

    return () => {
      cancelled = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      cancelPendingDisconnect();
      ++streamEpoch.current; stream.current?.(); retireFeedTarget.current?.();
    };
  }, [mode, reconnectKey, networkReconnectKey, replayPath]);

  // 历史卡的结局 (#932 P2, R4): a card replayed as waiting long after its confirm window reads its own record.
  useProposalSettlement({ state, sessionId, connected: connection === "connected", baseUrl, dispatch });

  const submitMessage = async (text: string, workspaceContext?: FuturesWorkspacePromptContext, extras?: MessageExtras) => {
    if (!sessionId) throw new Error("应用服务尚未连接");
    await (extras ? sendMessage(baseUrl, sessionId, text, workspaceContext, extras) : sendMessage(baseUrl, sessionId, text, workspaceContext));
  };

  const showBacktest = (card: BacktestComparison) => {
    dispatch({ type: "local-backtest", card });
  };

  return { state, changeChat, listChats, chatBusy, workspaceScope, rootWorkspaceScope, sessionId, connection, authRequired, runtimeActivityAt, baseUrl, rootBaseUrl, submitMessage, showBacktest };
}
