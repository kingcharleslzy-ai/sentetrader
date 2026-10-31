import { useState } from "react";
import { requestJson } from "../api";
import { strategyScope, type InstrumentScope, type SavedStrategy } from "../strategyApi";

export function StrategyScopeFields({ value, onChange, disabled }: {value: InstrumentScope; onChange: (scope: InstrumentScope) => void; disabled?: boolean}) {
  return <div className="strategy-scope"><label>适用范围<select value={value.mode} disabled={disabled} onChange={e => onChange({...value, mode: e.target.value as InstrumentScope["mode"]})}><option value="any">全部品种（不绑定）</option><option value="selected">指定一个或多个品种 / 合约</option></select></label>
    {value.mode === "selected" && <label>品种或合约代码<input value={value.symbols.join(",")} disabled={disabled} onChange={e => onChange({...value, symbols: e.target.value.split(/[，,\n]/)})} placeholder="例如 SHFE.au,SHFE.rb2610"/></label>}
  </div>;
}
export function StrategyScopeEditor({item, baseUrl, onSaved}: {item: SavedStrategy; baseUrl: string; onSaved: (item: SavedStrategy) => void}) {
  const [value, setValue] = useState(() => strategyScope(item));
  const [busy, setBusy] = useState(false); const [message, setMessage] = useState("");
  return <div><StrategyScopeFields value={value} onChange={setValue} disabled={busy}/><button type="button" disabled={busy} onClick={async () => {setBusy(true); setMessage(""); try {const saved = await requestJson<SavedStrategy>(`${baseUrl}/api/strategy-plans/${item.strategyId}/scope`, {method: "POST", body: JSON.stringify(value)}); onSaved(saved); setMessage("适用范围已保存，正在运行的合约保持不变。");} catch {setMessage("保存失败，请检查品种或合约代码。");} finally {setBusy(false);}}}>保存适用范围</button>{message && <p role="status">{message}</p>}</div>;
}
