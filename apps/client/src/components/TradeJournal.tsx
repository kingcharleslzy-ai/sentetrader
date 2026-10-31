import { useEffect, useState } from "react";
import type { PnlRound } from "../accountPnl";
import { realized } from "../accountPnl";
import { requestJson } from "../api";
import { TAG_LABELS } from "../dailyReview";
import type { TradeNote, UnlinkedNote } from "../hooks/useTradeReview";
import { useIdentityFence } from "../hooks/useIdentityFence";
import { useDisclosureSurface } from "../uiSurfaces";
import { SectionCard, StatTiles } from "./Blocks";
import { TagChip } from "./DailyReview";
import { signClass } from "./futures/support";
import { fillSourceLabel } from "./futures/PositionPanels";
import { formatNumber, formatSigned } from "./futures/support";
import { usePlaybooks } from "./playbooks/PlaybookPage";
import "./TradeJournal.css";

export function holdingLabel(minutes: number | undefined): string {
  if (minutes === undefined) return "—";
  if (minutes < 60) return `${minutes} 分钟`;
  if (minutes < 24 * 60) return `${Math.floor(minutes / 60)} 小时${minutes % 60 ? ` ${minutes % 60} 分` : ""}`;
  return `${Math.round(minutes / (24 * 60))} 天`;
}

const VIA = { typed: "手写", voice: "语音复盘时说的", chat: "对话里 AI 帮你记的" } as const;

/** Saves (or, empty, deletes) a round trip's 补充 through the route the AI's set_trade_note uses; the saved note or null. */
export async function saveTradeNote(baseUrl: string, accountId: string | null | undefined, roundId: string, text: string): Promise<TradeNote | null> {
  const url = `${baseUrl}/api/account/trade-notes/${encodeURIComponent(roundId)}`;
  const headers = accountId ? { "X-AI-Trader-Account": accountId } : undefined;
  if (text) return (await requestJson<{ note: TradeNote }>(url, { method: "PUT", headers, body: JSON.stringify({ text, via: "typed" }) })).note;
  const removed = await requestJson<{ deleted: boolean }>(url, { method: "DELETE", headers });
  if (!removed.deleted) throw new Error("note_not_deleted");
  return null;
}

/**
 * 交易日志 · 这笔交易 (2026-09-29): the selected round trip on the 复盘 page — result, who placed it and
 * why, what the research desk said when it was opened, and the user's 补充 (one line, saved through
 * PUT /api/account/trade-notes/{id}, the route the AI's set_trade_note uses too).
 */
export function TradeJournalCard({ trade, baseUrl, accountId, note, onNoteSaved, onShowAll, question }: {
  trade: PnlRound; baseUrl: string; accountId?: string | null; note: TradeNote | null;
  onNoteSaved: (id: string, note: TradeNote | null) => void; onShowAll: () => void;
  /** 交易验尸 (2026-10-25): the 今日复盘's question for this round trip asks in place of “你的补充”. */
  question?: string;
}) {
  const fence = useIdentityFence([baseUrl, accountId, trade.id]);
  const headers = accountId ? { "X-AI-Trader-Account": accountId } : undefined;
  const [draft, setDraft] = useState(note?.text ?? "");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  // The saved note arriving must not wipe the “已保存 / 已删除” it just produced: the status clears
  // only when another trade is selected.
  useEffect(() => { setDraft(note?.text ?? ""); }, [trade.id, note?.text]);
  useEffect(() => { setStatus(""); }, [trade.id]);
  const save = async () => {
    const text = draft.trim();
    setBusy(true); setStatus("");
    try {
      const saved = await saveTradeNote(baseUrl, accountId, trade.id, text);
      if (!fence.isCurrent()) return;
      onNoteSaved(trade.id, saved); setStatus(saved ? "已保存" : "已删除");
    } catch { if (fence.isCurrent()) setStatus("没有保存成功，请重试"); }
    finally { if (fence.isCurrent()) setBusy(false); }
  };
  // 打法本 (2026-10-25): tag this round trip with a setup afterwards (overrides the card's); shown until the next poll.
  const playbooks = usePlaybooks(baseUrl);
  const [tagged, setTagged] = useState<{ id: string; playbookId: string } | null>(null);
  const playbookShown = tagged?.id === trade.id ? tagged.playbookId : trade.playbookId ?? "";
  const tagPlaybook = async (playbookId: string) => {
    const tagUrl = `${baseUrl}/api/account/trade-playbooks/${encodeURIComponent(trade.id)}`;
    setTagged({ id: trade.id, playbookId }); setStatus("");
    try {
      await requestJson(tagUrl, playbookId ? { method: "PUT", headers, body: JSON.stringify({ playbookId }) } : { method: "DELETE", headers });
      if (fence.isCurrent()) setStatus("打法已记下");
    } catch { if (fence.isCurrent()) { setTagged(null); setStatus("打法没有记下，请重试"); } }
  };
  const value = realized(trade);
  const desk = trade.deskView;
  const source = fillSourceLabel({ source: trade.source ?? "manual", strategyName: trade.strategyName }) ?? "手动";
  const changed = draft.trim() !== (note?.text ?? "");
  return <SectionCard title="这笔交易" note={`${trade.symbol} · ${trade.direction === "long" ? "多" : "空"} ${trade.volume} 手`}
    className="trade-journal" actions={<button type="button" onClick={onShowAll}>显示全图</button>}>
    <div className="section-card-body">
      <StatTiles columns={2} label="这笔交易的结果" items={[
        { label: trade.net !== null ? "净收益" : "毛收益", value: <b className={signClass(value)}>{formatSigned(value, 0)}</b>,
          note: trade.fees !== null ? `手续费 ${formatNumber(trade.fees, 2)}` : undefined },
        { label: "持仓", value: holdingLabel(trade.holdingMinutes) },
        { label: "开 → 平", value: `${formatNumber(trade.openPrice, 2)} → ${formatNumber(trade.closePrice, 2)}` },
        { label: "来源", value: source },
      ]} />
      <dl className="trade-journal-rows">
        {trade.openTag && trade.closeTag ? <div><dt>标签</dt><dd className="trade-journal-tags">
          <TagChip tag={trade.openTag} label={`开 · ${TAG_LABELS[trade.openTag]}`} />
          <TagChip tag={trade.closeTag} label={`平 · ${TAG_LABELS[trade.closeTag]}`} /></dd></div> : null}
        {playbooks.items.length ? <div><dt>打法</dt><dd>
          <select className="app-select" aria-label="这笔的打法" value={playbookShown} onChange={event => void tagPlaybook(event.target.value)}>
            <option value="">不标</option>
            {playbooks.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select></dd></div>
          : trade.playbookName ? <div><dt>打法</dt><dd>{trade.playbookName}</dd></div> : null}
        <div><dt>理由</dt><dd>{trade.reason || (trade.source === "strategy" ? `策略「${source}」的信号` : "—")}</dd></div>
        <div><dt>当时的调研判断</dt><dd>{desk?.headline ?? <span className="trade-journal-muted">没有留档</span>}</dd></div>
      </dl>
      <label className="trade-journal-note">
        <span>{question ?? "你的补充"}{note ? <small>{VIA[note.via] ?? ""}</small> : null}</span>
        <textarea rows={2} maxLength={500} value={draft} placeholder="当时怎么想的、学到了什么（一句话就行）"
          onChange={event => { setDraft(event.target.value); setStatus(""); }} />
      </label>
      <div className="trade-journal-actions">
        <button type="button" className="strategy-primary" disabled={busy || !changed} onClick={() => void save()}>
          {draft.trim() || !note ? "保存补充" : "删除补充"}</button>
        {status ? <span role="status">{status}</span> : null}
      </div>
    </div>
  </SectionCard>;
}

const noteTime = (value: string) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
};

/**
 * 未关联的补充 (2026-10-04, #514): 补充 whose backfilled fill a real report replaced without proving it
 * was the same trade, so no round trip carries them. Folded at the end of the 复盘 page (disclosure
 * `unlinked-notes`); each can be read or deleted under its old key — never re-attached by guess.
 */
export function UnlinkedNotes({ notes, baseUrl, accountId }: { notes: UnlinkedNote[]; baseUrl: string; accountId?: string | null }) {
  const fence = useIdentityFence([baseUrl, accountId]);
  const [removed, setRemoved] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [failed, setFailed] = useState(false);
  const shown = notes.filter(note => !removed.includes(note.roundId));
  const [open, setOpen] = useDisclosureSurface("unlinked-notes", { available: shown.length > 0 });
  if (!shown.length) return null;
  const remove = async (roundId: string) => {
    setBusy(roundId); setFailed(false);
    try {
      const result = await requestJson<{ deleted: boolean }>(`${baseUrl}/api/account/trade-notes/${encodeURIComponent(roundId)}`, { method: "DELETE",
        headers: accountId ? { "X-AI-Trader-Account": accountId } : undefined });
      if (!fence.isCurrent()) return;
      if (!result.deleted) throw new Error("note_not_deleted");
      setRemoved(current => [...current, roundId]);
    } catch { if (fence.isCurrent()) setFailed(true); }
    finally { if (fence.isCurrent()) setBusy(""); }
  };
  return <SectionCard title={`未关联的补充（${shown.length}）`} label="未关联的补充" className="trade-journal-unlinked"
    note="对应的成交已被真实回报替换，无法确定是哪一笔"
    actions={<button type="button" aria-expanded={open} aria-label={open ? "收起未关联的补充" : "展开未关联的补充"}
      onClick={() => setOpen(!open)}>{open ? "收起" : "展开"}</button>}>
    {open && <div className="section-card-body">
      <ul className="trade-journal-unlinked-list">
        {shown.map(note => <li key={note.roundId}>
          <p>{note.text}</p>
          <small>{VIA[note.via] ?? note.via} · {noteTime(note.updatedAt)}</small>
          <button type="button" disabled={busy === note.roundId} aria-label={`删除未关联的补充：${note.text.slice(0, 20)}`}
            onClick={() => void remove(note.roundId)}>删除</button>
        </li>)}
      </ul>
      {failed ? <p className="trade-journal-muted" role="status">没有删除成功，请重试</p> : null}
    </div>}
  </SectionCard>;
}
