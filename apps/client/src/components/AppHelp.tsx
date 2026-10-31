import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { requestJson } from "../api";
import { isRequestTimeout } from "../requestTimeout";
import { startTour, type TourStep } from "../uiTour";
import { useDialogSurface, useUiSurface } from "../uiSurfaces";
import "./AppHelp.css";

/**
 * 使用说明 (2026-09-25 两层重做): a search box and grouped navigation on the left (a drop-down on a
 * narrow card), the topic on the right — title, one-sentence summary, numbered 怎么用, 常见问题 that
 * open one by one, and 完整说明 (the complete facts agents also read) folded at the bottom.
 * Commandable as `help: {topic, group, query, detail, faq}` and `dialog: help`; observed as `help`.
 */
export function openAppHelp(topic = "tour") {
  window.dispatchEvent(new CustomEvent("ai-trader-help", { detail: topic }));
}
export function HelpButton({ topic = "tour", onClick }: { topic?: string; onClick?: () => void }) {
  return <button type="button" aria-label="查看说明" title="查看说明" onClick={onClick ?? (() => openAppHelp(topic))}>?</button>;
}

interface HelpTopic {
  title: string;
  group: string;
  summary: string;
  steps: string[];
  faq: { q: string; a: string }[];
  terms?: { term: string; plain: string; when: string }[];
  detail: string[];
  tour?: TourStep[];
}
interface HelpGroup { id: string; title: string; ownerOnly?: boolean; collapsed?: boolean }
interface HelpData { groups: HelpGroup[]; aliases: Record<string, string>; topics: Record<string, HelpTopic> }
type RawHelp = { groups?: HelpGroup[]; aliases?: Record<string, string>; topics: Record<string, Partial<HelpTopic> & { title: string; paragraphs?: string[] }> };
export interface HelpChange { topic?: string; group?: string; query?: string; detail?: boolean; faq?: { index: number; open: boolean }; retry?: boolean }

const DEFAULT_TOPIC = "tour";
const LEGACY_GROUP = "all";

/** A Runtime before the two-layer manual sends only titles and paragraphs: show them as the complete text. */
function normalize(raw: RawHelp): HelpData {
  const topics: Record<string, HelpTopic> = {};
  for (const [id, item] of Object.entries(raw.topics)) {
    topics[id] = { title: item.title, group: item.group ?? LEGACY_GROUP, summary: item.summary ?? "", steps: item.steps ?? [],
      faq: item.faq ?? [], terms: item.terms, detail: item.detail ?? item.paragraphs ?? [], tour: item.tour };
  }
  return { groups: raw.groups ?? [{ id: LEGACY_GROUP, title: "全部说明" }], aliases: raw.aliases ?? {}, topics };
}

const lower = (text: string) => text.toLocaleLowerCase();
const hit = (text: string, query: string) => Boolean(query) && lower(text).includes(query);

function topicMatches(item: HelpTopic, query: string) {
  return [item.title, item.summary, ...item.steps, ...item.faq.flatMap(entry => [entry.q, entry.a]),
    ...(item.terms ?? []).flatMap(term => [term.term, term.plain, term.when]), ...item.detail].some(text => hit(text, query));
}

/** The text with every occurrence of the search term marked. */
function Marked({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const parts: ReactNode[] = [];
  const source = lower(text);
  let from = 0;
  for (let at = source.indexOf(query); at >= 0; at = source.indexOf(query, from)) {
    if (at > from) parts.push(text.slice(from, at));
    parts.push(<mark key={at}>{text.slice(at, at + query.length)}</mark>);
    from = at + query.length;
  }
  parts.push(text.slice(from));
  return <>{parts.map((part, index) => <Fragment key={index}>{part}</Fragment>)}</>;
}

export function AppHelp({ baseUrl, isOwner = false }: { baseUrl?: string; isOwner?: boolean }) {
  // The requested topic (any id or old alias); undefined = the card is closed.
  const [requested, setRequested] = useState<string>();
  const [data, setData] = useState<HelpData>();
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [query, setQueryText] = useState("");
  // undefined = automatic (open when the search hits it); a click or command overrides.
  const [detailOverride, setDetailOverride] = useState<boolean>();
  const [faqOverride, setFaqOverride] = useState<Record<number, boolean>>({});
  const [developerOpen, setDeveloperOpen] = useState(false);
  const [menu, setMenu] = useState<{ x: number; y: number; topic: string }>();
  const dialog = useRef<HTMLDialogElement>(null);

  const resetFolds = () => { setDetailOverride(undefined); setFaqOverride({}); };
  const select = (topic: string) => { setRequested(topic); resetFolds(); setMenu(undefined); };
  const setQuery = (text: string) => { setQueryText(text); resetFolds(); };
  const close = () => { setRequested(undefined); setError(""); };

  const groups = (data?.groups ?? []).filter(group => isOwner || !group.ownerOnly);
  const topicIds = data ? Object.keys(data.topics).filter(id => groups.some(group => group.id === data.topics[id].group)) : [];
  const resolve = (id: string | undefined) => {
    if (!data || id === undefined) return undefined;
    const target = data.aliases[id] ?? id;
    return topicIds.includes(target) ? target : undefined;
  };
  const needle = lower(query.trim());
  const matches = needle && data ? topicIds.filter(id => topicMatches(data.topics[id], needle)) : topicIds;
  const resolved = resolve(requested) ?? resolve(DEFAULT_TOPIC) ?? topicIds[0];
  // A search shows its first hit when the open topic does not match.
  const current = requested === undefined ? undefined : needle && matches.length && resolved && !matches.includes(resolved) ? matches[0] : resolved;
  const selected = current && data ? data.topics[current] : undefined;
  const group = selected ? groups.find(item => item.id === selected.group) : undefined;
  const developerShown = developerOpen || Boolean(group?.collapsed) || Boolean(needle && data && matches.some(id => groups.find(item => item.id === data.topics[id].group)?.collapsed));
  const detailOpen = selected ? detailOverride ?? (!selected.summary || selected.detail.some(text => hit(text, needle))) : false;
  const faqOpen = (index: number) => {
    const entry = selected?.faq[index];
    return Boolean(entry && (faqOverride[index] ?? (hit(entry.q, needle) || hit(entry.a, needle))));
  };

  useDialogSurface("help", dialog, { open: next => select(next || DEFAULT_TOPIC), close });
  useEffect(() => {
    const open = (event: Event) => {
      setRequested((event as CustomEvent<string>).detail || DEFAULT_TOPIC);
      setDetailOverride(undefined); setFaqOverride({}); setMenu(undefined);
    };
    const context = (event: MouseEvent) => {
      if (event.defaultPrevented || !(event.target instanceof Element)) return;
      const target = event.target.closest<HTMLElement>("[data-help]");
      if (!target?.dataset.help) return;
      event.preventDefault();
      setMenu({ x: Math.min(event.clientX, window.innerWidth - 170), y: Math.min(event.clientY, window.innerHeight - 60), topic: target.dataset.help });
    };
    const dismiss = () => setMenu(undefined);
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") dismiss(); };
    window.addEventListener("ai-trader-help", open);
    window.addEventListener("contextmenu", context);
    window.addEventListener("click", dismiss);
    window.addEventListener("keydown", key);
    return () => { window.removeEventListener("ai-trader-help", open); window.removeEventListener("contextmenu", context); window.removeEventListener("click", dismiss); window.removeEventListener("keydown", key); };
  }, []);
  const isOpen = requested !== undefined;
  useEffect(() => {
    const element = dialog.current;
    if (isOpen && element && !element.open) element.showModal();
    else if (!isOpen && element?.open) element.close();
  }, [isOpen]);
  // Keep the open topic visible in the navigation (a command or a search can pick one far down).
  useEffect(() => { dialog.current?.querySelector<HTMLElement>(".app-help-group [aria-current=page]")?.scrollIntoView?.({ block: "nearest" }); }, [current, developerShown]);
  // The manual is read once per session; a failed or timed-out read is retried from 重试 or the next open.
  useEffect(() => {
    if (!isOpen || data || baseUrl === undefined) return;
    let cancelled = false;
    setError("");
    void requestJson<RawHelp>(baseUrl + "/api/app/help").then(value => { if (!cancelled) setData(normalize(value)); })
      .catch(reason => { if (!cancelled) setError(isRequestTimeout(reason) ? "说明读取超时，应用服务暂时没有响应" : "说明读取失败，应用服务暂时不可用"); });
    return () => { cancelled = true; };
  }, [isOpen, data, baseUrl, attempt]);

  // Where the manual read stands, for the card and the help surface's snapshot.
  const loadStatus = data ? "ready" : baseUrl === undefined ? "offline" : error ? "error" : "loading";
  const retryRead = () => { setError(""); setAttempt(value => value + 1); };
  const apply = (value: unknown) => {
    const change = value as HelpChange;
    // help: {retry: true} = the card's 重试 (only after a failed read; a closed card opens and reads anyway).
    if (change.retry) {
      if (data) throw new Error("说明已读到，不用重新读取");
      if (baseUrl === undefined) throw new Error("应用服务未连接，连接后可查看说明");
      // An open card reading right now has no 重试 to press; a second parallel read is refused.
      if (isOpen && loadStatus === "loading") throw new Error("说明正在读取，稍候再试");
      if (isOpen) retryRead();
    }
    if (change.topic !== undefined) {
      if (data && !resolve(change.topic)) throw new Error(data.aliases[change.topic] ?? data.topics[change.topic] ? "该说明仅主账号可见" : "没有这个帮助主题");
      select(change.topic);
    } else if (change.group !== undefined) {
      const target = groups.find(item => item.id === change.group);
      if (data && !target) throw new Error("没有这个帮助分组");
      const first = data ? topicIds.find(id => data.topics[id].group === change.group) : undefined;
      if (target?.collapsed) setDeveloperOpen(true);
      select(first ?? current ?? DEFAULT_TOPIC);
    } else if (requested === undefined) select(DEFAULT_TOPIC);
    if (change.query !== undefined) setQuery(change.query);
    if (change.detail !== undefined) setDetailOverride(change.detail);
    if (change.faq) {
      const { index, open } = change.faq;
      if (change.topic === undefined && change.group === undefined && selected && index >= selected.faq.length) throw new Error("没有这个常见问题");
      setFaqOverride(previous => ({ ...previous, [index]: open }));
    }
  };
  const faqIndexes = selected ? selected.faq.map((_, index) => index).filter(faqOpen) : [];
  useUiSurface("help", {
    state: { open: isOpen, topic: isOpen ? current ?? null : null, group: isOpen ? selected?.group ?? null : null, query,
      matches: needle ? matches : [], detailOpen, faqOpen: faqIndexes, developerOpen: developerShown,
      loadStatus, loadError: error || null, canRetry: loadStatus === "error" },
    apply,
  }, [isOpen, requested, current, selected, query, needle, matches.join(), detailOpen, faqIndexes.join(), developerShown, data, isOwner, loadStatus, error, baseUrl]);

  const topicButton = (id: string) => data && <li key={id}><button type="button" aria-current={id === current ? "page" : undefined} onClick={() => select(id)}>
    <Marked text={data.topics[id].title} query={needle} /></button></li>;
  return <>
    {menu && <div className="app-help-menu" role="menu" style={{ left: menu.x, top: menu.y }}><button type="button" role="menuitem" onClick={() => openAppHelp(menu.topic)}>查看相关说明</button></div>}
    <dialog ref={dialog} className="app-help-card" onCancel={close} aria-label="使用说明">
      <header><strong>使用说明</strong><button type="button" aria-label="关闭说明" onClick={close}>×</button></header>
      {error ? <p role="alert" className="app-help-status">{error}<button type="button" className="ghost-button" data-agent-target="help:retry" onClick={retryRead}>重试</button></p>
        : baseUrl === undefined && !data ? <p className="app-help-status">应用服务未连接，连接后可查看说明</p>
        : !data || !selected ? <p className="app-help-status">正在读取说明…</p> : <div className="app-help-body">
        <aside className="app-help-nav">
          <input type="search" className="app-help-search" placeholder="搜索说明" aria-label="搜索说明" value={query} onChange={event => setQuery(event.target.value)} />
          <select className="app-help-picker" aria-label="选择主题" value={current} onChange={event => select(event.target.value)}>
            {groups.map(item => <optgroup key={item.id} label={item.title}>
              {matches.filter(id => data.topics[id].group === item.id).map(id => <option key={id} value={id}>{data.topics[id].title}</option>)}
            </optgroup>)}
          </select>
          <nav aria-label="说明目录">
            {groups.map(item => {
              const ids = matches.filter(id => data.topics[id].group === item.id);
              if (!ids.length) return null;
              if (item.collapsed) return <section key={item.id} className="app-help-group">
                <button type="button" className="app-help-group-toggle" aria-expanded={developerShown} onClick={() => setDeveloperOpen(!developerShown)}>{item.title}</button>
                {developerShown && <ul>{ids.map(topicButton)}</ul>}
              </section>;
              return <section key={item.id} className="app-help-group"><h4>{item.title}</h4><ul>{ids.map(topicButton)}</ul></section>;
            })}
            {needle && !matches.length && <p className="app-help-empty">没有找到“{query.trim()}”相关的说明</p>}
          </nav>
        </aside>
        <article className="app-help-topic" aria-label={selected.title} key={current}>
          <h3><Marked text={selected.title} query={needle} /></h3>
          {selected.summary && <p className="app-help-summary"><Marked text={selected.summary} query={needle} /></p>}
          {selected.steps.length > 0 && <section aria-label="怎么用"><h4>怎么用</h4><ol className="app-help-steps">
            {selected.steps.map(step => <li key={step}><Marked text={step} query={needle} /></li>)}</ol></section>}
          {selected.tour?.length ? <p><button type="button" className="app-help-tour" data-agent-target="help:start-tour" onClick={() => { close(); startTour(selected.tour as TourStep[]); }}>开始引导（{selected.tour.length} 步）</button></p> : null}
          {selected.terms?.length ? <section aria-label="术语"><h4>术语</h4><dl className="app-help-terms">
            {selected.terms.map(term => <div key={term.term}><dt><Marked text={term.term} query={needle} /></dt>
              <dd><Marked text={term.plain} query={needle} /><small>什么时候会碰到：<Marked text={term.when} query={needle} /></small></dd></div>)}</dl></section> : null}
          {selected.faq.length > 0 && <section aria-label="常见问题"><h4>常见问题</h4><div className="app-help-faq">
            {selected.faq.map((entry, index) => { const open = faqOpen(index); return <div key={entry.q} className="app-help-faq-item">
              <button type="button" aria-expanded={open} onClick={() => setFaqOverride(previous => ({ ...previous, [index]: !open }))}><span><Marked text={entry.q} query={needle} /></span></button>
              {open && <p><Marked text={entry.a} query={needle} /></p>}
            </div>; })}</div></section>}
          {selected.detail.length > 0 && <section className="app-help-detail" aria-label="完整说明">
            <button type="button" className="app-help-detail-toggle" aria-expanded={detailOpen} onClick={() => setDetailOverride(!detailOpen)}>完整说明</button>
            {detailOpen && <div className="app-help-detail-body">{selected.detail.map(text => <p key={text}><Marked text={text} query={needle} /></p>)}</div>}
          </section>}
        </article>
      </div>}
    </dialog>
  </>;
}
