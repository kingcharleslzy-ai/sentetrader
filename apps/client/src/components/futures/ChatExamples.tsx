/** Suggestions only prefill the owning composer; sending stays an explicit user action. */
export function ChatExamples({ items, onFill }: { items: string[]; onFill: (text: string) => void }) {
  return items.length ? <div className="fdw-chat-examples" data-agent-target="chat-examples" aria-label="建议问题" key={items.join("\n")}>
    {items.map(text => <button type="button" key={text} title={`填入输入框：${text}`} onClick={() => onFill(text)}>{text}</button>)}
  </div> : null;
}
