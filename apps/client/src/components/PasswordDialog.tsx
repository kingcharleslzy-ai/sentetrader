import { useEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { useDialogSurface } from "../uiSurfaces";
import "./PasswordDialog.css";

export const PASSWORD_MIN = 10;

const FAILURES: Record<string, string> = {
  invalid_current_password: "当前密码不对",
  invalid_password: `新密码至少 ${PASSWORD_MIN} 位`,
  password_unchanged: "新密码不能和当前密码相同",
  password_managed_by_tqkq: "这个账号用快期登录，密码请在快期修改",
  user_unknown: "没有这个账号",
};

function failureText(failure: unknown) {
  const { status, detail } = (failure ?? {}) as { status?: number; detail?: { detail?: unknown } };
  if (status === 429) return "尝试次数过多，请稍后再试";
  const code = typeof detail?.detail === "string" ? detail.detail : "";
  return FAILURES[code] ?? "没有改成，请检查网络后重试";
}

/**
 * 修改密码 / 重置密码 (2026-10-21): one form for 设置 → 账户 (当前密码 + 新密码) and 后台 → 成员
 * (新密码 only). A commandable dialog the AI can open and close but never fill: the snapshot skips
 * input values and no command types into them — submitting stays the person's click.
 */
export function PasswordDialog({ surface, open, onOpenChange, title, subject, needCurrent, note, submitLabel, onSubmit, closeOnly }: {
  surface: "change-password" | "admin-reset-password";
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  /** The account the password belongs to, shown under the title. */
  subject: string;
  needCurrent: boolean;
  /** What happens to sessions afterwards — said before the click. */
  note: string;
  submitLabel: string;
  onSubmit: (currentPassword: string, newPassword: string) => Promise<void>;
  /** The admin reset needs a member picked by a click; commands can only close it. */
  closeOnly?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  useDialogSurface(surface, ref, { open: () => onOpenChange(true), close: () => onOpenChange(false), closeOnly });
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
    if (!open) { setCurrent(""); setNext(""); setAgain(""); setError(undefined); }
  }, [open]);
  const mismatch = again.length > 0 && again !== next;
  const tooShort = next.length > 0 && next.length < PASSWORD_MIN;
  const ready = (!needCurrent || current.length > 0) && next.length >= PASSWORD_MIN && again === next && !busy;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(undefined);
    try {
      await onSubmit(current, next);
      onOpenChange(false);
    } catch (failure) {
      setError(failureText(failure));
    } finally {
      setBusy(false);
    }
  };
  return createPortal(<dialog ref={ref} className="sheet password-dialog" aria-labelledby={`${surface}-title`}
    onCancel={event => { if (busy) event.preventDefault(); else onOpenChange(false); }}>
    <form onSubmit={event => void submit(event)} noValidate>
      <h3 id={`${surface}-title`}>{title}</h3>
      <p className="password-dialog-subject">{subject}</p>
      {needCurrent ? <label className="credential-field">
        <span>当前密码</span>
        <input aria-label="当前密码" type="password" autoComplete="current-password" value={current}
          onChange={event => setCurrent(event.target.value)} disabled={busy} autoFocus />
      </label> : null}
      <label className="credential-field">
        <span>新密码</span>
        <input aria-label="新密码" type="password" autoComplete="new-password" placeholder={`至少 ${PASSWORD_MIN} 位`} value={next}
          onChange={event => setNext(event.target.value)} disabled={busy} autoFocus={!needCurrent} />
      </label>
      <label className="credential-field">
        <span>再输一次</span>
        <input aria-label="再输一次新密码" type="password" autoComplete="new-password" value={again}
          onChange={event => setAgain(event.target.value)} disabled={busy} />
      </label>
      {tooShort ? <p className="password-dialog-hint">新密码至少 {PASSWORD_MIN} 位</p>
        : mismatch ? <p className="password-dialog-hint">两次输入的新密码不一样</p> : null}
      <p className="password-dialog-note">{note}</p>
      {error ? <p className="credential-warning" role="alert">{error}</p> : null}
      <div className="approval-actions">
        <button className="ghost-button" type="button" onClick={() => onOpenChange(false)} disabled={busy}>取消</button>
        <button className="primary-button accent" type="submit" disabled={!ready}>{busy ? "正在保存…" : submitLabel}</button>
      </div>
    </form>
  </dialog>, document.body);
}
