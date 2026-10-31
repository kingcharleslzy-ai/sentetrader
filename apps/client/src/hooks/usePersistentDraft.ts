import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";

const changed = "ai-trader-draft-changed";
function read<T>(key: string | undefined, fallback: T): T {
  if (!key) return fallback;
  try { return JSON.parse(localStorage.getItem(key) || "null")?.value ?? fallback; }
  catch { return fallback; }
}

/** Save synchronously before navigation; server/model results may arrive after unmount. */
export function usePersistentDraft<T>(key: string | undefined, initial: T) {
  const fallback = useRef(initial);
  const [state, setState] = useState(() => ({ key, value: read(key, initial) }));
  const [saveError, setSaveError] = useState(false);
  const failed = useRef(false);
  const current = useRef(state);
  current.current = state;
  useEffect(() => {
    failed.current = false;
    const reload = (event?: Event) => {
      if (failed.current || (event instanceof CustomEvent && event.detail !== key) || (event instanceof StorageEvent && event.key !== key)) return;
      setState({ key, value: read(key, fallback.current) });
    };
    reload();
    window.addEventListener(changed, reload);
    window.addEventListener("storage", reload);
    return () => { window.removeEventListener(changed, reload); window.removeEventListener("storage", reload); };
  }, [key]);
  const update = useCallback((action: SetStateAction<T>) => {
    const cached = current.current.key === key ? current.current.value : fallback.current;
    const previous = failed.current ? cached : read(key, cached);
    const next = typeof action === "function" ? (action as (value: T) => T)(previous) : action;
    if (key) {
      try { localStorage.setItem(key, JSON.stringify({ value: next })); failed.current = false; setSaveError(false); }
      catch { failed.current = true; setSaveError(true); }
    }
    current.current = { key, value: next };
    setState(current.current);
    if (key && !failed.current) window.dispatchEvent(new CustomEvent(changed, {detail: key}));
  }, [key]);
  return [state.key === key ? state.value : read(key, initial), update, saveError] as const;
}
