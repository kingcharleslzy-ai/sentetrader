import { useEffect, useState } from "react";
import { strategyText, type Clarification } from "../strategyApi";

export function StrategyQuestions({ questions, answers, round, busy, onChange, onContinue }: {
  questions: Clarification[]; answers: Record<string, string>; round: number; busy: boolean;
  onChange: (id: string, answer: string) => void; onContinue: () => void;
}) {
  const [index, setIndex] = useState(0);
  const identity = JSON.stringify(questions.map(q => [q.id, q.question]));
  useEffect(() => setIndex(0), [identity, round]);
  const q = questions[Math.min(index, questions.length - 1)];
  if (!q) return null;
  return <section className="strategy-clarifications" aria-label="完善策略">
    <header><strong>确认条件 · {index + 1} / {questions.length}</strong><p>{q.options.length ? "选一项，或填写自己的想法；建议不会自动选中。" : "这条旧草稿还没有可选建议，可点击上方生成建议，也可直接填写。"}</p></header>
    <fieldset disabled={busy} key={q.id}><legend>{strategyText(q.question)}</legend>
      {q.options.map((o, i) => <label className="strategy-choice" key={o.label}><input type="radio" name={`clarify-${q.id}`} checked={answers[q.id] === o.label} onChange={() => onChange(q.id, o.label)}/><span><strong>{strategyText(o.label)}{i === q.recommended ? "（建议）" : ""}</strong><small>{strategyText(o.description)}</small></span></label>)}
      <label>自己补充<textarea rows={2} maxLength={2000} value={q.options.some(o => o.label === answers[q.id]) ? "" : answers[q.id] || ""} onChange={e => onChange(q.id, e.target.value)} placeholder="也可以用自己的话说明"/></label>
    </fieldset>
    <div className="strategy-question-nav"><button type="button" disabled={busy || index === 0} onClick={() => setIndex(i => i - 1)}>上一项</button>{index < questions.length - 1 ? <button type="button" disabled={busy || !answers[q.id]?.trim()} onClick={() => setIndex(i => i + 1)}>下一项</button> : <button type="button" disabled={busy || questions.some(q => !answers[q.id]?.trim())} onClick={onContinue}>回答并完善策略</button>}</div>
  </section>;
}
