import { useCallback, useEffect, useState } from "react";
import { deletePlaybook, listPlaybooks, savePlaybook, type Playbook, type PlaybookFields } from "../../api";
import { useDisclosureSurface } from "../../uiSurfaces";
import { SectionCard } from "../Blocks";
import { Button, Select, TextInput } from "../settings/controls";
import "./PlaybookPage.css";

const CHANGED = "ai-trader-playbooks-changed";

/** 打法本 (2026-10-25): the user's setups, read once per window and re-read whenever one is saved or deleted
 *  here (the ticket's 打法 choice reads the same list). */
export function usePlaybooks(baseUrl: string | undefined) {
  const [state, setState] = useState<{ items: Playbook[]; max: number; error: string; loaded: boolean }>({ items: [], max: 20, error: "", loaded: false });
  const reload = useCallback(() => {
    if (!baseUrl) return;
    // A Runtime from before 打法本 (or anything else answering) has no list: no setups, not a crash.
    void listPlaybooks(baseUrl).then(result => setState({ items: Array.isArray(result?.playbooks) ? result.playbooks : [],
      max: typeof result?.max === "number" ? result.max : 20, error: "", loaded: true }))
      .catch(cause => setState(current => ({ ...current, error: cause instanceof Error ? cause.message : "打法读取失败", loaded: true })));
  }, [baseUrl]);
  useEffect(() => {
    reload();
    window.addEventListener(CHANGED, reload);
    return () => window.removeEventListener(CHANGED, reload);
  }, [reload]);
  return state;
}

const EMPTY: PlaybookFields = { name: "", entryRules: "", exitRules: "", stopRules: "", strategyId: null };
const HINT = "打法是你自己的一种做法：什么时候进、什么时候走、止损放哪。下单时选上它，复盘就能按打法看胜率，验尸时也拿它对照你有没有照做。可以关联一个策略，让程序替你核对规则。";

function Rules({ label, value, onChange, max }: { label: string; value: string; onChange: (value: string) => void; max: number }) {
  return <label className="playbook-field"><span>{label}</span>
    <textarea className="app-text-input" aria-label={label} value={value} maxLength={max} rows={2} onChange={event => onChange(event.target.value)} /></label>;
}

/** The 策略 panel's 打法 tab: one block per setup with its three rule lines; 新建打法 / 改 open the editor
 *  (`disclosure: playbook-editor`); 删 asks once more. */
export function PlaybookPage({ baseUrl, strategies }: { baseUrl: string; strategies: { strategyId: string; name: string }[] }) {
  const { items, max, error, loaded } = usePlaybooks(baseUrl);
  const [editorOpen, setEditorOpen] = useDisclosureSurface("playbook-editor");
  const [editing, setEditing] = useState<string | null>(null);
  const [fields, setFields] = useState<PlaybookFields>(EMPTY);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const strategyName = (id: string | null) => strategies.find(item => item.strategyId === id)?.name;
  const open = (playbook?: Playbook) => {
    setEditing(playbook?.id ?? null);
    setFields(playbook ? { name: playbook.name, entryRules: playbook.entryRules, exitRules: playbook.exitRules, stopRules: playbook.stopRules, strategyId: playbook.strategyId } : EMPTY);
    setProblem("");
    setEditorOpen(true);
  };
  const save = async () => {
    setSaving(true); setProblem("");
    try {
      await savePlaybook(baseUrl, { ...fields, strategyId: fields.strategyId || null }, editing ?? undefined);
      setEditorOpen(false);
      window.dispatchEvent(new Event(CHANGED));
    } catch (cause) { setProblem(cause instanceof Error ? cause.message : "没有保存，请重试"); }
    finally { setSaving(false); }
  };
  const remove = async (id: string) => {
    setConfirmDelete(null);
    try { await deletePlaybook(baseUrl, id); window.dispatchEvent(new Event(CHANGED)); }
    catch (cause) { setProblem(cause instanceof Error ? cause.message : "没有删掉，请重试"); }
  };
  const set = (key: keyof PlaybookFields) => (value: string) => setFields(current => ({ ...current, [key]: value }));
  return <div className="playbook-page" data-agent-target="strategy-playbooks">
    <SectionCard title="打法本" hint={HINT} helpKey="playbooks"
      actions={editorOpen ? undefined : <Button primary disabled={items.length >= max} onClick={() => open()}>新建打法</Button>}>
      {error ? <p role="alert">{error}</p> : null}
      {loaded && !items.length && !editorOpen ? <p className="playbook-quiet">还没有打法，先写下你最常用的一种</p> : null}
      {items.length >= max ? <p className="playbook-quiet">打法最多 {max} 个，删掉一个才能再加</p> : null}
    </SectionCard>
    {editorOpen ? <SectionCard title={editing ? "改打法" : "新建打法"}>
      <div className="playbook-editor">
        <label className="playbook-field"><span>名字</span><TextInput label="打法名字" value={fields.name} maxLength={20} onChange={set("name")} /></label>
        <Rules label="入场规则" value={fields.entryRules} max={300} onChange={set("entryRules")} />
        <Rules label="出场规则" value={fields.exitRules} max={300} onChange={set("exitRules")} />
        <Rules label="止损规则" value={fields.stopRules} max={200} onChange={set("stopRules")} />
        <label className="playbook-field"><span>关联策略</span>
          <Select label="关联策略" value={fields.strategyId ?? ""} onChange={value => setFields(current => ({ ...current, strategyId: value || null }))}
            options={[{ value: "", label: "不关联" }, ...strategies.map(item => ({ value: item.strategyId, label: item.name }))]} /></label>
        {problem ? <p role="alert">{problem}</p> : null}
        <div className="playbook-actions">
          <Button primary disabled={saving || !fields.name.trim()} onClick={() => void save()}>保存</Button>
          <Button disabled={saving} onClick={() => setEditorOpen(false)}>取消</Button>
        </div>
      </div>
    </SectionCard> : null}
    {items.map(item => <SectionCard key={item.id} title={item.name} testId={`playbook:${item.id}`}
      actions={<span className="playbook-actions">
        <Button onClick={() => open(item)}>改</Button>
        {confirmDelete === item.id
          ? <Button danger onClick={() => void remove(item.id)}>确认删除</Button>
          : <Button danger onClick={() => setConfirmDelete(item.id)}>删</Button>}
      </span>}>
      <dl className="playbook-rules">
        <dt>入场</dt><dd>{item.entryRules || "—"}</dd>
        <dt>出场</dt><dd>{item.exitRules || "—"}</dd>
        <dt>止损</dt><dd>{item.stopRules || "—"}</dd>
        {item.strategyId ? <><dt>策略</dt><dd>{strategyName(item.strategyId) ?? "已删除的策略"}</dd></> : null}
      </dl>
    </SectionCard>)}
  </div>;
}
