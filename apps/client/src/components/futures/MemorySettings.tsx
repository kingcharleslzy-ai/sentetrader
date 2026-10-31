import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { requestJson } from "../../api";
import { readNetworkMode, readQueryClient, useReadIdentity } from "../../runtimeClient";
import { SettingRow, SettingsSection } from "../SettingsPanel";
import { Button, Select, Switch } from "../settings/controls";
import { useUiSurface } from "../../uiSurfaces";
import type { PreferencesHandle } from "../../userPreferences";
import "./MemorySettings.css";

/** 设置 → 记忆 (记忆层 3a, 2026-09-29; 设置拆页 2026-09-30): the private memory in three
 * layers — every item shows where it came from, edits inline, deletes (失效) and the last
 * changes can be undone. Reference only, never an authorization. The main page shows the
 * 画像 layer only; 约定、经验与最近变动 live in the memory-advanced second-level page; both
 * share one data-loading hook so the surface state stays continuous across the two pages. */
interface MemoryItem {
  id: string; layer: "profile" | "rule" | "lesson"; layerLabel: string; text: string;
  source: string; evidence: { via?: string; quote?: string | null; at?: string; n?: number };
  tags: string[]; status: string; createdAt: string; invalidAt: string | null; invalidReason: string | null;
}

/** POST /api/memory/consolidate 的结果：程序统计的计数 + 从原话提炼落地的操作数。HTTP 200 也可能
 *  只完成一半：某段 `{error}`（提炼的模型调用失败，当天记 error 待重试）或 `{skipped}`（写入前复核
 *  不过——记忆被关掉 disabled、这次整理被接手 cancelled——零写入；no_words 是没有新话可提炼）。 */
interface ConsolidateResult {
  stats?: { added?: number; updated?: number; invalidated?: number; skipped?: number | string; error?: string };
  words?: { applied?: number; ops?: { add?: number; update?: number; invalidate?: number }; skipped?: string; error?: string };
}

const SKIP_REASONS: Record<string, string> = {
  disabled: "记忆在整理途中被关掉，这次没有写入",
  cancelled: "这次整理被新的一次接手，以新的一次为准",
};

/** The result line and, when a part did not finish, the reason (shown as the error line). */
function consolidationOutcome(result: ConsolidateResult | undefined): { summary: string; problem: string } {
  const stats = result?.stats ?? {};
  const words = result?.words ?? {};
  const ops = { ...{ add: 0, update: 0, invalidate: 0 }, ...words.ops };
  const counts = {
    add: (stats.added ?? 0) + ops.add,
    update: (stats.updated ?? 0) + ops.update,
    invalidate: (stats.invalidated ?? 0) + ops.invalidate,
  };
  const total = counts.add + counts.update + counts.invalidate;
  const problems: string[] = [];
  if (stats.error) problems.push("交易统计没有算完，稍后再试");
  if (words.error) problems.push("从你说的话里提炼没有完成，稍后再试");
  for (const skipped of [stats.skipped, words.skipped]) {
    const reason = typeof skipped === "string" ? SKIP_REASONS[skipped] : undefined;
    if (reason && !problems.includes(reason)) problems.push(reason);
  }
  const summary = total ? `记下 ${counts.add} 条、更新 ${counts.update} 条、失效 ${counts.invalidate} 条`
    : problems.length ? "" : "没有新东西要记";
  return { summary, problem: problems.join("；") };
}

interface MemoryChange {
  id: string; memoryId: string; op: "add" | "update" | "invalidate" | "restore";
  actor: "user" | "ai" | "consolidation"; at: string;
  before: MemoryItem | null; after: MemoryItem | null;
}

interface MemoryDocument { enabled: boolean; items: MemoryItem[]; changes: MemoryChange[] }

const LAYERS = [
  { value: "profile", label: "画像" },
  { value: "rule", label: "约定" },
  { value: "lesson", label: "经验" },
] as const;

/** 最近变动的动词：谁做的（你 / AI / 自动整理）和做了什么（add/update/invalidate/restore）。 */
function changeLabel(change: MemoryChange): string {
  const { op, actor } = change;
  if (actor === "consolidation") {
    const verbs: Record<string, string> = { add: "记下", update: "修改", invalidate: "失效", restore: "恢复" };
    return `自动整理：${verbs[op] ?? op}`;
  }
  if (op === "add") return actor === "ai" ? "AI 记下" : "你添加";
  if (op === "update") return "你修改";
  if (op === "invalidate") return actor === "ai" ? "AI 删除" : "你删除";
  return "已恢复";
}

/** “M月D日” from the Runtime's ISO stamp (Beijing for chat evidence, UTC for rows — a date label only). */
function dayLabel(stamp: string | undefined): string {
  if (!stamp) return "";
  const date = new Date(stamp);
  return Number.isNaN(date.getTime()) ? "" : `${date.getMonth() + 1}月${date.getDate()}日`;
}

function evidenceLine(item: MemoryItem): string {
  if (item.source === "stats") {
    return typeof item.evidence.n === "number" ? `交易统计 · 近 90 天 ${item.evidence.n} 笔` : "交易统计";
  }
  const day = dayLabel(item.evidence.at ?? item.createdAt);
  if (item.evidence.via === "chat" && item.evidence.quote) return `你说：「${item.evidence.quote}」· ${day}`;
  if (item.evidence.via === "consolidation" && item.evidence.quote) {
    return `整理自你说的：「${item.evidence.quote}」· ${day}`;
  }
  return `设置里填写 · ${day}`;
}

const memoryKey = (identity: number, baseUrl: string | undefined) => ["memory", identity, baseUrl] as const;

/** The two memory pages' shared state: one cached read of the document for both pages (a write
 *  or 整理 on either page refreshes whichever page shows it), its read state, the shared error
 *  surface and the `memory` UI-command state (so the snapshot keeps reporting while either page
 *  is open). A failed read never reads as an empty memory: the first failure says so, a failed
 *  refresh keeps the list it had. */
function useMemoryDocument(baseUrl: string | undefined, offline: boolean, memoryOn: boolean) {
  const identity = useReadIdentity();
  const key = memoryKey(identity, baseUrl);
  // A newer read (after a write) cancels an older one in flight: a late old answer cannot bring
  // back an item deleted in between.
  const query = useQuery({ queryKey: key, enabled: !offline, networkMode: readNetworkMode(baseUrl),
    queryFn: ({ signal }) => requestJson<MemoryDocument>(`${baseUrl}/api/memory`, { signal }) }, readQueryClient);
  const [error, setError] = useState("");
  const reload = () => { void readQueryClient.invalidateQueries({ queryKey: key, exact: true }); };
  /** Runs one write, then rereads; resolves false (with the error shown) when the write failed. */
  const act = async (run: () => Promise<unknown>) => {
    setError("");
    try { await run(); } catch (cause) {
      const detail = (cause as { detail?: { detail?: unknown } }).detail?.detail;
      setError(typeof detail === "string" ? detail : "操作没有完成，请重试");
      return false;
    }
    reload();
    return true;
  };
  const document = query.data;
  const items = document?.items ?? [];
  const counts = { profile: 0, rule: 0, lesson: 0 };
  for (const item of items) counts[item.layer] += 1;
  useUiSurface("memory", { state: { enabled: memoryOn, counts } }, [memoryOn, counts.profile, counts.rule, counts.lesson]);
  const readState: MemoryReadState = document ? (query.isError ? "stale" : "ready") : query.isError ? "failed" : "loading";
  return { document, readState, error, setError, reload, act, items };
}

type MemoryReadState = "loading" | "ready" | "failed" | "stale";

/** The read's own line: nothing once read, 读取中 before, the failure (with 重试) after. */
function MemoryReadLine({ state, reload }: { state: MemoryReadState; reload: () => void }) {
  if (state === "ready") return null;
  if (state === "loading") return <span className="app-memory-empty">读取中…</span>;
  return <div className="app-memory-error app-memory-read-error" role="alert">
    <span>{state === "failed" ? "记忆没有读到，列表暂不可用" : "刷新没有成功，显示的是上次读到的内容"}</span>
    <Button onClick={reload}>重试</Button>
  </div>;
}

/** The inline editor shared by both pages: one item at a time; 保存 waits for its PUT (no second
 *  save meanwhile), and a landed save only closes the editor when the draft is still the text
 *  that was sent — a draft changed while it was saving stays open. A failed save keeps both. */
function useMemoryEditor(baseUrl: string | undefined, act: (run: () => Promise<unknown>) => Promise<boolean>) {
  const [editing, setEditing] = useState<{ id: string; text: string; version: number }>();
  const [savingId, setSavingId] = useState<string>();
  const save = (id: string) => {
    const sent = editing;
    if (!sent || sent.id !== id || savingId) return;
    setSavingId(id);
    void act(() => requestJson(`${baseUrl}/api/memory/${id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: sent.text }),
    })).then(saved => {
      if (saved) setEditing(current => current?.id === id && current.version === sent.version ? undefined : current);
    }).finally(() => setSavingId(undefined));
  };
  const remove = (id: string) => void act(() => requestJson(`${baseUrl}/api/memory/${id}`, { method: "DELETE" }));
  return {
    editing, savingId, save, remove,
    start: (item: MemoryItem) => setEditing({ id: item.id, text: item.text, version: 0 }),
    change: (text: string) => setEditing(current => current && { ...current, text, version: current.version + 1 }),
    cancel: () => setEditing(undefined),
  };
}

/** One layer's editable list (used by both pages; each page shows its own layers): the
 *  layer name is its own small heading, and every entry takes a full-width row — the text
 *  left-aligned and wrappable, its grey source line under it, 修改 / 删除 to the right. */
function MemoryLayerRows({ layer, items, editor }: {
  layer: "profile" | "rule" | "lesson"; items: MemoryItem[];
  editor: ReturnType<typeof useMemoryEditor>;
}) {
  const label = LAYERS.find(entry => entry.value === layer)!.label;
  const rows = items.filter(item => item.layer === layer);
  return <div className="app-memory-layer">
    <div className="app-memory-layer-title">{label}{layer === "lesson" ? <small>你亲口说过的经验；每条可标品种</small> : null}</div>
    <div className="app-memory-list">
      {rows.length === 0 ? <span className="app-memory-empty">还没有</span>
        : rows.map(item => (
          <div className="app-memory-item" key={item.id}>
            {editor.editing?.id === item.id
              ? <div className="app-memory-edit">
                  <input className="app-memory-input" aria-label={`修改${label}`} value={editor.editing.text} maxLength={120}
                    onChange={event => editor.change(event.target.value)} />
                  <Button disabled={editor.savingId === item.id} status={editor.savingId === item.id ? "保存中" : undefined}
                    onClick={() => editor.save(item.id)}>保存</Button>
                  <Button onClick={editor.cancel}>取消</Button>
                </div>
              : <>
                  <div className="app-memory-text">{item.text}</div>
                  <div className="app-memory-evidence">{evidenceLine(item)}</div>
                  <div className="app-memory-actions">
                    <Button onClick={() => editor.start(item)}>修改</Button>
                    <Button danger onClick={() => editor.remove(item.id)}>删除</Button>
                  </div>
                </>}
          </div>
        ))}
    </div>
  </div>;
}

/** 设置 → 记忆: the switch and the 画像 layer; everything else is in memory-advanced. */
export function MemorySettings({ baseUrl, preferences, setPref }: {
  baseUrl: string | undefined; preferences: PreferencesHandle;
  setPref: (key: string, value: unknown) => void;
}) {
  const offline = !baseUrl;
  const memoryOn = preferences.preferences.chat.memory ?? true;
  const { document, readState, error, reload, act, items } = useMemoryDocument(baseUrl, offline, memoryOn);
  const editor = useMemoryEditor(baseUrl, act);
  return <>
    <SettingsSection title="AI 记得的关于你" helpKey="memory-profile" help="只作参考，不构成任何授权；每条都能改和删">
      <SettingRow label="让 AI 记住我" hint={offline ? "未连接 Runtime，只读" : "关掉后对话与语音都不带记忆，也不接受对话里的“记住”"}>
        <Switch label="让 AI 记住我" checked={memoryOn} disabled={offline} onChange={value => setPref("chat.memory", value)} />
      </SettingRow>
      {offline ? null : <>
        {document ? <MemoryLayerRows layer="profile" items={items} editor={editor} /> : null}
        <MemoryReadLine state={readState} reload={reload} />
        {error ? <div className="app-memory-error" role="alert">{error}</div> : null}
      </>}
    </SettingsSection>
  </>;
}

/** 设置 → 记忆 → 高级记忆设置 (memory-advanced): 约定、经验、添加一条与最近变动。 */
export function MemoryAdvancedSettings({ baseUrl, preferences }: {
  baseUrl: string | undefined; preferences: PreferencesHandle;
}) {
  const offline = !baseUrl;
  const memoryOn = preferences.preferences.chat.memory ?? true;
  const { document, readState, error, setError, reload, act, items } = useMemoryDocument(baseUrl, offline, memoryOn);
  const editor = useMemoryEditor(baseUrl, act);
  const [draft, setDraft] = useState({ layer: "profile", text: "", version: 0 });
  const [consolidating, setConsolidating] = useState(false);
  const [summary, setSummary] = useState("");
  // One add at a time (a double click must not store the item twice); on success the draft is
  // cleared only when it is still the one that was sent — any edit meanwhile (text or layer) stays.
  const [adding, setAdding] = useState(false);
  const add = () => {
    if (adding) return;
    const sent = draft;
    setAdding(true);
    void act(async () => {
      await requestJson(`${baseUrl}/api/memory`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ layer: sent.layer, text: sent.text, via: "settings" }),
      });
      setDraft(current => current.version === sent.version ? { ...current, text: "", version: current.version + 1 } : current);
    }).finally(() => setAdding(false));
  };
  const undo = (changeId: string) => void act(() =>
    requestJson(`${baseUrl}/api/memory/changes/${changeId}/undo`, { method: "POST" }));

  // “现在整理一次”：与每天 06:00 的自动整理同一套逻辑，完成后说一句结果（没做完的部分说原因）
  // 再刷新——两页共用一份记忆，此时开着的那一页都会重读。
  const consolidate = () => {
    setConsolidating(true);
    setError("");
    setSummary("");
    requestJson<ConsolidateResult>(`${baseUrl}/api/memory/consolidate`, { method: "POST" }).then(result => {
      const outcome = consolidationOutcome(result);
      setSummary(outcome.summary);
      setError(outcome.problem);
      reload();
    }, cause => {
      const detail = (cause as { detail?: { detail?: unknown } }).detail?.detail;
      setError(typeof detail === "string" ? detail : "整理没有完成，请重试");
    }).finally(() => setConsolidating(false));
  };

  return <SettingsSection title="约定、经验与最近变动" helpKey="memory-layers" help="画像在「记忆」页；这里管其余两层和整理">
    {offline ? <SettingRow label="记忆"><strong>未连接 Runtime，只读</strong></SettingRow> : <>
      {document ? (["rule", "lesson"] as const).map(layer => (
        <MemoryLayerRows key={layer} layer={layer} items={items} editor={editor} />
      )) : null}
      <MemoryReadLine state={readState} reload={reload} />
      <SettingRow label="添加一条">
        <div className="app-memory-add">
          <Select label="记忆层级" value={draft.layer} options={LAYERS}
            onChange={layer => setDraft(current => ({ ...current, layer, version: current.version + 1 }))} />
          <input className="app-memory-input" aria-label="记忆内容" placeholder="比如：我只做螺纹和铁矿"
            maxLength={120} value={draft.text}
            onChange={event => { const text = event.target.value; setDraft(current => ({ ...current, text, version: current.version + 1 })); }} />
          <Button primary disabled={adding || !draft.text.trim()} onClick={add}>添加</Button>
        </div>
      </SettingRow>
      <SettingRow label="最近变动" helpKey="memory-changes" help="每条都可撤销">
        <div className="app-memory-changes">
          <div className="app-memory-consolidate">
            <Button disabled={consolidating} onClick={consolidate}>现在整理一次</Button>
            {summary ? <span className="app-memory-evidence">{summary}</span> : null}
          </div>
          {(document?.changes ?? []).slice(0, 5).map(change => (
            <div className="app-memory-change" key={change.id}>
              <span>{changeLabel(change)}</span>
              <span className="app-memory-change-text">{(change.after ?? change.before)?.text ?? ""}</span>
              <span className="app-memory-evidence">{dayLabel(change.at)}</span>
              <Button onClick={() => undo(change.id)}>撤销</Button>
            </div>
          ))}
        </div>
      </SettingRow>
      {error ? <div className="app-memory-error" role="alert">{error}</div> : null}
    </>}
  </SettingsSection>;
}
