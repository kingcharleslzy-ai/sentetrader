import { useCallback, useEffect, useRef, useState } from "react";
import { SettingRow, SettingsSection } from "../SettingsPanel";
import { useDisclosureSurface, useUiSurface } from "../../uiSurfaces";
import { deleteRoutine, listRoutines, routineError, ROUTINES_CHANGED, type RoutineItem, type RoutineList, type RoutineRef } from "../../routines";
import { ROUTINE_MANNER_WORDS } from "../cards/RoutineCards";
import "./RoutineSettings.css";

/** “MM-DD HH:mm” in the viewer's zone, for 上次运行. */
function runStamp(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** A step as one line: its words, then 只给建议 / 需要时执行 when the words leave it to judgment. */
const stepLine = (step: RoutineItem["steps"][number]) => `${step.text}${step.manner ? `（${ROUTINE_MANNER_WORDS[step.manner]}）` : ""}`;

/** The row's grey line: “8 步 · 上次 10-09 09:30”, or the first steps when it never ran. */
function routineHint(item: RoutineItem): string {
  const last = runStamp(item.lastRunAt);
  if (last) return `${item.stepCount} 步 · 上次 ${last}`;
  const head = item.steps.slice(0, 3).map(stepLine).join(" · ");
  return `${item.stepCount} 步：${head}${item.steps.length > 3 ? " …" : ""}`;
}

function RoutineRow({ item, baseUrl, onRun, onEdit, onDeleted, onError }: {
  item: RoutineItem; baseUrl: string; onRun: (routine: RoutineRef) => void; onEdit: (name: string) => void;
  onDeleted: () => void; onError: (message: string) => void;
}) {
  // 删 asks once more in place (disclosure: routine-delete, id = routine id); the AI can open or close
  // the question, the 确认删除 click stays the user's.
  const [confirming, setConfirming] = useDisclosureSurface("routine-delete", { id: item.id });
  const [busy, setBusy] = useState(false);
  const remove = () => {
    setBusy(true);
    deleteRoutine(baseUrl, item.id).then(() => { setConfirming(false); onDeleted(); },
      cause => onError(routineError(cause, "没有删掉，请重试"))).finally(() => setBusy(false));
  };
  return <SettingRow label={item.name} hint={routineHint(item)}>
    <div className="app-routine-actions">
      {confirming ? <>
        <button type="button" className="app-button danger" aria-label={`确认删除「${item.name}」`} disabled={busy} onClick={remove}>确认删除</button>
        <button type="button" className="app-button" aria-label={`取消删除「${item.name}」`} disabled={busy} onClick={() => setConfirming(false)}>取消</button>
      </> : <>
        <button type="button" className="app-button primary" aria-label={`运行「${item.name}」`} onClick={() => onRun({ id: item.id, name: item.name })}>运行</button>
        <button type="button" className="app-button" aria-label={`改「${item.name}」`} onClick={() => onEdit(item.name)}>改</button>
        <button type="button" className="app-button danger" aria-label={`删「${item.name}」`} onClick={() => setConfirming(true)}>删</button>
      </>}
    </div>
  </SettingRow>;
}

/**
 * 设置 → 记忆（手机 我的 → AI 与记忆）· 我的流程 (2026-10-06): the user's routines, one row each — name,
 * steps and 上次运行, then 运行 (sends “跑一下<名称>” in the chat, carrying the routine's id — #522), 改 (opens the chat with “改一下『名称』：”
 * filled in) and 删 (asks once more). Routines are made in the chat: the 流程卡's 保存 writes them.
 */
export function RoutineSettings({ baseUrl, onRun, onEdit }: {
  baseUrl: string; onRun: (routine: RoutineRef) => void; onEdit: (name: string) => void;
}) {
  const [list, setList] = useState<RoutineList>();
  const [error, setError] = useState("");
  const readSeq = useRef(0);
  const reload = useCallback(() => {
    const seq = ++readSeq.current;
    listRoutines(baseUrl).then(value => { if (seq === readSeq.current) { setList(value); setError(""); } },
      cause => { if (seq === readSeq.current) setError(routineError(cause, "我的流程暂时读不到")); });
  }, [baseUrl]);
  useEffect(reload, [reload]);
  useEffect(() => {
    window.addEventListener(ROUTINES_CHANGED, reload);
    return () => window.removeEventListener(ROUTINES_CHANGED, reload);
  }, [reload]);
  const items = list?.items ?? [];
  const limits = list?.limits ?? { routines: 5, steps: 10, nameChars: 12 };
  const summary = items.map(item => `${item.id}:${item.name}:${item.stepCount}:${item.lastRunAt ?? ""}`).join("|");
  useUiSurface("routines", { state: { count: items.length, items: items.map(item => ({ id: item.id, name: item.name,
    steps: item.steps.map(step => `${step.kindLabel}：${stepLine(step)}`), lastRunAt: item.lastRunAt })) } }, [summary]);
  return <SettingsSection title="我的流程" helpKey="routines" help="说名字或点“运行”，AI 按顺序做完；要下单的步骤只出卡等你确认">
    {list && !items.length ? <SettingRow label="还没有流程" helpKey="routines-empty" help="对 AI 说“以后我说『老规矩』就帮我……”，流程卡上点“保存”就存到这里"><span /></SettingRow> : null}
    {items.map(item => <RoutineRow key={item.id} item={item} baseUrl={baseUrl} onRun={onRun} onEdit={onEdit}
      onDeleted={reload} onError={setError} />)}
    <p className="app-routine-rules">最多 {limits.routines} 个流程、每个最多 {limits.steps} 步；改步骤直接跟 AI 说“老规矩去掉回测那步”。</p>
    {error ? <div className="app-memory-error" role="alert">{error}</div> : null}
  </SettingsSection>;
}
