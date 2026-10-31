import { useEffect, useRef, useState } from "react";
import { getAiExecution, requestJson, updateSimulationLimits, type AiExecutionControls } from "../api";
import type { IdentityFence } from "./useIdentityFence";
import type { SimulationLimitSettings } from "../components/FuturesDecisionWorkspace";

const defaults: SimulationLimitSettings = {enabled: false, maxOrderVolume: 10, maxOrders: 100};
type Draft = {key: string; limits: SimulationLimitSettings; pending: boolean; token: number; baseRevision: number | null};

function fromServer(control: AiExecutionControls): SimulationLimitSettings {
  const volume = control.maxOrderVolume && control.maxOrderVolume < 2147483647 ? control.maxOrderVolume : null;
  const orders = control.maxOrders && control.maxOrders < 2147483647 ? control.maxOrders : null;
  if (volume === null && orders === null) return defaults;
  return {enabled: volume !== null || orders !== null, maxOrderVolume: volume ?? 2147483647, maxOrders: orders ?? 2147483647};
}

export function useSimulationLimits({baseUrl, rootBaseUrl, scope, fence, connected, controls}: {
  baseUrl: string; rootBaseUrl: string; scope?: string; fence: IdentityFence; connected: boolean; controls?: AiExecutionControls;
}) {
  // Old global preferences cannot identify an owner or environment; never apply them automatically.
  const key = scope ? `ai-trader-simulation-limits-v2:${scope}` : undefined;
  const [draft, setDraft] = useState<Draft>();
  const context = fence.context;
  const [feedback, setFeedback] = useState<{key: string; context: object; text: string}>();
  const current = useRef({context, key});
  current.current = {context, key};
  const queue = useRef(Promise.resolve());
  const loaded = useRef<object | undefined>(undefined);
  const observed = useRef<{ context: object; revision: number } | undefined>(undefined);
  const token = useRef(0);
  const isCurrent = () => current.current.context === context && current.current.key === key && fence.isCurrent();

  useEffect(() => {
    if (!key) {setDraft(undefined); return;}
    let saved: Partial<Draft> | null = null;
    try { saved = JSON.parse(localStorage.getItem(key) || "null"); } catch { /* Invalid local drafts are ignored. */ }
    const valid = saved?.limits && Number.isSafeInteger(saved.limits.maxOrderVolume) && saved.limits.maxOrderVolume > 0 && saved.limits.maxOrderVolume <= 2147483647
      && Number.isSafeInteger(saved.limits.maxOrders) && saved.limits.maxOrders > 0 && saved.limits.maxOrders <= 2147483647;
    setDraft({key, limits: valid ? saved!.limits! : defaults, pending: Boolean(valid && saved?.pending === true), token: ++token.current,
      baseRevision: Number.isSafeInteger(saved?.baseRevision) && saved!.baseRevision! >= 0 ? saved!.baseRevision! : null});
    loaded.current = undefined;
    observed.current = undefined;
  }, [key]);

  useEffect(() => {
    if (!key || draft?.key !== key || !connected || (!draft.pending && loaded.current === context)) return;
    let cancelled = false;
    const active = () => !cancelled && isCurrent();
    // Serialize settings writes, including fast edits. Never retry a trading action.
    queue.current = queue.current.catch(() => undefined).then(async () => {
      if (!active()) return;
      setFeedback({key, context, text: draft.pending ? "同步中" : "读取中"});
      try {
        const owner = await requestJson<{scope: string}>(`${rootBaseUrl}/api/workspace`);
        if (!active()) return;
        if (!scope?.startsWith(`${owner.scope}:`)) throw new Error("账户归属已变化");
        const snapshot = await getAiExecution(baseUrl);
        if (!active()) return;
        if (!snapshot.controls) throw new Error("当前服务尚不支持独立限额设置");
        observed.current = { context, revision: snapshot.controls.revision };
        const desiredVolume = draft.limits.enabled ? draft.limits.maxOrderVolume : null;
        const desiredOrders = draft.limits.enabled ? draft.limits.maxOrders : null;
        const alreadyApplied = snapshot.controls.maxOrderVolume === desiredVolume && snapshot.controls.maxOrders === desiredOrders;
        if (draft.pending && !alreadyApplied && draft.baseRevision !== snapshot.controls.revision) {
          setFeedback({key, context, text: "限额已变化或编辑基准缺失，草稿已保留；确认后重新保存"});
          return;
        }
        const control = draft.pending && !alreadyApplied ? await updateSimulationLimits(baseUrl, {
          revision: draft.baseRevision!,
          maxOrderVolume: draft.limits.enabled ? draft.limits.maxOrderVolume : null,
          maxOrders: draft.limits.enabled ? draft.limits.maxOrders : null,
        }) : snapshot.controls;
        if (!active()) return;
        const next: Draft = {...draft, limits: fromServer(control), pending: false, baseRevision: control.revision};
        loaded.current = context;
        observed.current = { context, revision: control.revision };
        localStorage.setItem(key, JSON.stringify(next));
        setDraft(next);
        setFeedback({key, context, text: "已生效"});
      } catch {
        if (active()) setFeedback({key, context, text: "同步失败，请重新保存"});
      }
    });
    return () => {cancelled = true;};
  }, [key, context, connected, draft, baseUrl, rootBaseUrl, scope]);

  // External Agent writes use the business API; reflect newer server settings
  // without replacing a local unsaved draft or replaying a write.
  useEffect(() => {
    if (!key || !controls || !connected || !isCurrent() || draft?.key !== key || draft.pending
        || loaded.current !== context || observed.current?.context !== context
        || controls.revision <= observed.current.revision) return;
    observed.current = { context, revision: controls.revision };
    const next = { ...draft, limits: fromServer(controls), baseRevision: controls.revision };
    localStorage.setItem(key, JSON.stringify(next));
    setDraft(next);
    setFeedback({ key, context, text: "已生效" });
  }, [key, controls, connected, context, draft]);

  const change = (limits: SimulationLimitSettings) => {
    if (!key || !isCurrent()) return;
    const next: Draft = {key, limits, pending: true, token: ++token.current,
      baseRevision: observed.current?.context === context ? observed.current.revision : draft?.key === key ? draft.baseRevision : null};
    localStorage.setItem(key, JSON.stringify(next));
    setDraft(next);
    setFeedback({key, context, text: "待同步"});
  };
  return {
    limits: draft && draft.key === key ? draft.limits : defaults,
    status: !key ? "连接后可设置" : !connected && draft?.pending ? "待同步" : feedback?.key === key && feedback.context === context ? feedback.text : "待读取",
    change,
  };
}
