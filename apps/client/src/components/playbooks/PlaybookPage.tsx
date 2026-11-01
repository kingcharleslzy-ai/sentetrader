import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { deletePlaybook, listPlaybooks, savePlaybook, type Playbook, type PlaybookFields } from "../../api";
import { readNetworkMode, readQueryClient, useReadIdentity } from "../../runtimeClient";
import { useStrategies } from "../../strategyApi";
import { useDisclosureSurface } from "../../uiSurfaces";
import { SectionCard } from "../Blocks";
import { Button, Select, TextInput } from "../settings/controls";
import "./PlaybookPage.css";

const NO_PLAYBOOKS: Playbook[] = [];
const playbooksChanged = () => readQueryClient.invalidateQueries({ queryKey: ["playbooks"] });

/** 打法本 (2026-10-25): the user's setups, one cache entry per Runtime, re-read whenever one is saved or deleted
 *  here (the ticket's 打法 choice reads the same entry). */
export function usePlaybooks(baseUrl: string | undefined) {
  const identity = useReadIdentity();
  const query = useQuery({ queryKey: ["playbooks", identity, baseUrl], enabled: Boolean(baseUrl), networkMode: readNetworkMode(baseUrl),
    queryFn: () => listPlaybooks(baseUrl!) }, readQueryClient);
  // A Runtime from before 打法本 (or anything else answering) has no list: no setups, not a crash.
  const result = query.data;
  return { items: Array.isArray(result?.playbooks) ? result.playbooks : NO_PLAYBOOKS, max: typeof result?.max === "number" ? result.max : 20,
    error: query.error ? (query.error instanceof Error ? query.error.message : "交易笔记读取失败") : "", loaded: query.isFetched };
}

const EMPTY: PlaybookFields = { name: "", entryRules: "", exitRules: "", stopRules: "", strategyId: null };
const HINT = "交易笔记写你自己手动做单的一种做法：什么时候进、什么时候走、止损放哪。下单时选上它，复盘就能按笔记看胜率，验尸时也拿它对照你有没有照做。它不会自动下单；想让程序盯信号，用策略。可以关联一个策略，让程序替你核对规则。";

function Rules({ label, value, onChange, max }: { label: string; value: string; onChange: (value: string) => void; max: number }) {
  return <label className="playbook-field"><span>{label}</span>
    <textarea className="app-text-input" aria-label={label} value={value} maxLength={max} rows={2} onChange={event => onChange(event.target.value)} /></label>;
}

/** 交易笔记 (was the 策略 panel's 打法 tab; on 复盘 since 改版第一步 2026-10-31): one block per setup with its three rule
 *  lines; 新建笔记 / 改 open the editor (`disclosure: playbook-editor`); 删 asks once more. */
export function PlaybookPage({ baseUrl }: { baseUrl: string }) {
  const { items, max, error, loaded } = usePlaybooks(baseUrl);
  // 关联策略: the user's strategies, not the 只提醒 ones; a failed read only leaves the choice at 不关联.
  const strategies = (useStrategies(baseUrl).data?.items ?? []).filter(item => !item.definition.plan?.reminderOnly);
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
      void playbooksChanged();
    } catch (cause) { setProblem(cause instanceof Error ? cause.message : "没有保存，请重试"); }
    finally { setSaving(false); }
  };
  const remove = async (id: string) => {
    setConfirmDelete(null);
    try { await deletePlaybook(baseUrl, id); void playbooksChanged(); }
    catch (cause) { setProblem(cause instanceof Error ? cause.message : "没有删掉，请重试"); }
  };
  const set = (key: keyof PlaybookFields) => (value: string) => setFields(current => ({ ...current, [key]: value }));
  return <div className="playbook-page" data-agent-target="review-playbooks">
    <SectionCard title="交易笔记" hint={HINT} helpKey="playbooks"
      actions={editorOpen ? undefined : <Button primary disabled={items.length >= max} onClick={() => open()}>新建笔记</Button>}>
      {error ? <p role="alert">{error}</p> : null}
      {loaded && !items.length && !editorOpen ? <p className="playbook-quiet">还没有交易笔记，先写下你最常用的一种做法</p> : null}
      {items.length >= max ? <p className="playbook-quiet">交易笔记最多 {max} 条，删掉一条才能再加</p> : null}
    </SectionCard>
    {editorOpen ? <SectionCard title={editing ? "改笔记" : "新建笔记"}>
      <div className="playbook-editor">
        <label className="playbook-field"><span>名字</span><TextInput label="笔记名字" value={fields.name} maxLength={20} onChange={set("name")} /></label>
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
