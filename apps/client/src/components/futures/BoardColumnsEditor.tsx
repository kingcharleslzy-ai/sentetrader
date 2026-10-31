import { BOARD_COLUMNS_MAX, BOARD_COLUMN_LABELS, BOARD_COLUMN_TYPES, DEFAULT_BOARD_COLUMNS, type BoardColumn } from "./support";

/**
 * 报价总表列设置 (2026-10-02): one row per selectable column — 勾选框 (shown) + 列名 + 上移/下移 —
 * in the current display order, the columns not shown after them in their canonical order. Checking
 * appends at the end; 恢复默认 returns to the eight fixed-era columns. The board's 列 popover and
 * 设置 → 界面 → 报价总表 render this one component, so both save the same complete
 * `marketBoard.columns` list through the account preference path (guests keep it local).
 */
export function BoardColumnsEditor({ columns, onChange, status }: {
  columns: BoardColumn[];
  onChange: (columns: BoardColumn[]) => void;
  /** The save path's inline status (已保存 / 错误原因); the settings page and the popover share it. */
  status?: string;
}) {
  const shown = columns;
  const hidden = BOARD_COLUMN_TYPES.filter(id => !shown.includes(id));
  const setColumns = (next: BoardColumn[]) => onChange(next.length ? next : [...shown]);
  const toggle = (id: BoardColumn, checked: boolean) => {
    if (checked) setColumns([...shown, id]);
    else setColumns(shown.filter(item => item !== id));
  };
  const move = (id: BoardColumn, delta: -1 | 1) => {
    const index = shown.indexOf(id);
    const target = index + delta;
    if (index < 0 || target < 0 || target >= shown.length) return;
    const next = [...shown];
    next.splice(target, 0, next.splice(index, 1)[0]);
    setColumns(next);
  };
  const row = (id: BoardColumn, shownAt: number) => {
    const label = BOARD_COLUMN_LABELS[id];
    const full = shown.length >= BOARD_COLUMNS_MAX;
    const last = shown.length <= 1;
    const blocked = shownAt < 0 ? (full ? `最多 ${BOARD_COLUMNS_MAX} 列` : undefined) : (last ? "至少保留一列" : undefined);
    return <div className="fdw-board-cols-row" key={id}>
      <input type="checkbox" aria-label={label} checked={shownAt >= 0} disabled={blocked !== undefined}
        title={blocked} onChange={event => toggle(id, event.target.checked)} />
      <span className="fdw-board-cols-name">{label}</span>
      <span className="fdw-board-cols-move">
        <button type="button" aria-label={`上移${label}`} disabled={shownAt <= 0} onClick={() => move(id, -1)}>上移</button>
        <button type="button" aria-label={`下移${label}`} disabled={shownAt < 0 || shownAt >= shown.length - 1} onClick={() => move(id, 1)}>下移</button>
      </span>
    </div>;
  };
  return <div className="fdw-board-cols-editor">
    {shown.map((id, index) => row(id, index))}
    {hidden.map(id => row(id, -1))}
    <div className="fdw-board-cols-footer">
      <button type="button" onClick={() => onChange([...DEFAULT_BOARD_COLUMNS])}>恢复默认</button>
      {status ? <span role="status">{status}</span> : null}
    </div>
  </div>;
}
