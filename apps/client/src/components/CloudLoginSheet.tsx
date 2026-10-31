import { useEffect, useRef, useState, type FormEvent } from "react";

interface Props {
  open: boolean;
  onClose: () => void;
  /** The one login (owner 2026-10-04): site accounts and TqKq accounts alike go to `POST /api/auth/tqkq`. */
  onLogin: (username: string, password: string, inviteCode: string) => Promise<void>;
  /** Creates a site account (`POST /api/auth/owner/register`): from the 注册 form, or after the
   *  person confirmed “创建新账号？” for a login name nobody has. Enables the 注册 link. */
  onRegister?: (username: string, password: string, inviteCode: string) => Promise<void>;
  /** 本站模拟盘: a new account comes with a site-hosted simulation account. */
  paperConfigured?: boolean;
  defaultUsername?: string;
  required?: boolean;
}

interface LoginFailure {
  status?: number;
  code?: string;
  reason?: string;
  canRegister?: boolean;
}

/** `requestJson` errors read `"<status> <body>"`; the body is FastAPI's `{"detail": …}`. */
function readFailure(error: unknown): LoginFailure {
  const message = error instanceof Error ? error.message : String(error ?? "");
  const match = /^(\d{3}) ([\s\S]*)$/.exec(message);
  if (!match) return {};
  let detail: unknown;
  try { detail = (JSON.parse(match[2]) as { detail?: unknown }).detail; } catch { detail = undefined; }
  if (typeof detail === "string") return { status: Number(match[1]), code: detail };
  if (detail && typeof detail === "object") {
    const body = detail as Record<string, unknown>;
    return {
      status: Number(match[1]),
      code: typeof body.code === "string" ? body.code : undefined,
      reason: typeof body.reason === "string" ? body.reason : undefined,
      canRegister: body.canRegister === true,
    };
  }
  return { status: Number(match[1]) };
}

/** A new name that is not a TqKq account either: offer to create it (a TqKq timeout is not "not found"). */
function offersRegistration(failure: LoginFailure) {
  if (!failure.canRegister) return false;
  return failure.code === "account_not_found"
    || (failure.code === "tqkq_login_failed" && (failure.reason ?? "auth_failed") === "auth_failed");
}

function describeLoginFailure(failure: LoginFailure, inviteTyped: boolean) {
  if (failure.status === 429) return "尝试次数过多，请稍后再试";
  switch (failure.code) {
    case "tqkq_login_failed":
      if (failure.reason === "timeout") return "快期连接超时，稍后再试";
      if (failure.reason === "capacity_full") return "服务器在线人数已满，请稍后再试";
      return inviteTyped ? "账号或密码不对" : "账号或密码不对；新账号首次登录需要填写邀请码";
    case "account_not_found":
      return inviteTyped ? "没有找到这个账号，当前未开放新账号注册" : "没有找到这个账号；新账号首次登录需要填写邀请码";
    case "invalid_owner_credentials":
      return "账号或密码不对";
    case "invite_code_invalid":
      return inviteTyped ? "邀请码无效或已被使用" : "新账号首次登录需要填写邀请码";
    case "account_disabled":
      return "该账号已停用，请联系管理员";
    case "use_owner_login":
      return "这是管理员的快期账号，请用管理员账号和密码登录";
    case "username_taken":
      return "该账号名不可用，请换一个";
    case "invalid_username":
      return "账号需 3-64 位字母、数字或 _ . @ + - 组合";
    case "invalid_password":
      return "请输入密码";
    default:
      return "登录服务暂时不可用，请稍后再试";
  }
}

function describeRegisterFailure(failure: LoginFailure) {
  if (failure.status === 429) return "尝试次数过多，请稍后再试";
  switch (failure.code) {
    case "invite_code_invalid": return "邀请码无效或已被使用";
    case "invalid_invite_code": return "请填写邀请码";
    case "username_taken": return "账号名已被使用";
    case "invalid_username": return "新账号名需 3-32 位字母、数字、下划线或连字符";
    case "invalid_password": return "新账号的密码至少 10 位";
    case "registration_disabled": return "当前未开放新账号注册";
    default: return "注册服务暂时不可用，请稍后再试";
  }
}

/** Site sign-up rules, as `OwnerAuth.register` checks them. */
const NEW_USERNAME = /^[A-Za-z0-9_-]{3,32}$/;
const MIN_PASSWORD = 10;
/** How long 正在登录… waits before it says the server is still answering. */
const SLOW_AUTH_MS = 10_000;

export function CloudLoginSheet({
  open, onClose, onLogin, onRegister, paperConfigured = false, defaultUsername = "", required = false,
}: Props) {
  // 注册 (2026-10-05): our own sign-up form in the same sheet replaces the link to TqKq's.
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState(defaultUsername);
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [confirmingRegister, setConfirmingRegister] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  useEffect(() => {
    if (!open) return;
    setMode("login");
    setUsername(defaultUsername);
    setPassword("");
    setConfirmPassword("");
    setInviteCode("");
    setConfirmingRegister(false);
    setError(undefined);
  }, [defaultUsername, open]);
  // 仍在等待 (#710 B): a sign-in or sign-up still pending after a while says so. No retry: these POSTs
  // consume invites, create accounts and count attempts, so the original request's answer is the one that applies.
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    setSlow(false);
    if (!busy) return;
    const timer = window.setTimeout(() => setSlow(true), SLOW_AUTH_MS);
    return () => window.clearTimeout(timer);
  }, [busy]);
  // Esc (#734): an optional login closes like 取消 (not while a request is pending); a required one
  // stays. Either way the key stops here so the workspace behind the modal does not act on it. The
  // element that opened the sheet is read before the form's autoFocus moves focus and gets it back on close.
  const opener = useRef<HTMLElement | null>(null);
  if (open && !opener.current && typeof document !== "undefined") {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : document.body;
  }
  const escapeClose = useRef<() => void>(() => undefined);
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      escapeClose.current();
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      const target = opener.current;
      opener.current = null;
      if (target?.isConnected && target !== document.body) target.focus();
    };
  }, [open]);
  if (!open) return null;
  const slowLine = slow && busy ? <p className="cloud-login-slow" role="status">还在等服务器响应，请稍候</p> : null;
  const close = () => {
    if (busy || required) return;
    setMode("login");
    setPassword("");
    setConfirmPassword("");
    setInviteCode("");
    setConfirmingRegister(false);
    setError(undefined);
    onClose();
  };
  escapeClose.current = close;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !username.trim() || !password) return;
    setBusy(true);
    setError(undefined);
    try {
      await onLogin(username.trim(), password, inviteCode.trim());
      setPassword("");
      setInviteCode("");
    } catch (loginError) {
      const failure = readFailure(loginError);
      if (onRegister && offersRegistration(failure)) {
        // Keep the typed credentials: confirming creates the account with exactly these.
        setConfirmingRegister(true);
      } else {
        setPassword("");
        setError(describeLoginFailure(failure, Boolean(inviteCode.trim())));
      }
    } finally {
      setBusy(false);
    }
  };
  const register = async () => {
    if (busy || !onRegister) return;
    setBusy(true);
    setError(undefined);
    try {
      await onRegister(username.trim(), password, inviteCode.trim());
      setPassword("");
      setInviteCode("");
      setConfirmingRegister(false);
    } catch (registerError) {
      setPassword("");
      setConfirmingRegister(false);
      setError(describeRegisterFailure(readFailure(registerError)));
    } finally {
      setBusy(false);
    }
  };
  const backToForm = () => {
    if (busy) return;
    setConfirmingRegister(false);
  };
  const switchMode = (next: "login" | "register") => {
    if (busy) return;
    setMode(next);
    setPassword("");
    setConfirmPassword("");
    setInviteCode("");
    setError(undefined);
  };
  const submitRegistration = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !onRegister) return;
    const name = username.trim();
    if (!NEW_USERNAME.test(name)) { setError("账号需 3–32 位字母、数字、下划线或连字符"); return; }
    if (password.length < MIN_PASSWORD) { setError(`密码至少 ${MIN_PASSWORD} 位`); return; }
    if (password !== confirmPassword) { setError("两次输入的密码不一致"); return; }
    if (!inviteCode.trim()) { setError("请填写邀请码"); return; }
    setBusy(true);
    setError(undefined);
    try {
      await onRegister(name, password, inviteCode.trim());
      setPassword("");
      setConfirmPassword("");
      setInviteCode("");
    } catch (registerError) {
      setError(describeRegisterFailure(readFailure(registerError)));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={`sheet-backdrop approval-backdrop${required ? " required-login" : ""}`} role="presentation" onMouseDown={close}>
      <section data-agent-private className="sheet approval-sheet" role="dialog" aria-modal="true" aria-labelledby="cloud-login-title" onMouseDown={(event) => event.stopPropagation()}>
        <span className="eyebrow">Sente 账户</span>
        {confirmingRegister ? (
          <>
            <h2 id="cloud-login-title">创建新账号？</h2>
            <p>没有找到账号 <strong>{username.trim()}</strong>。用这个账号和密码创建新账号？</p>
            {paperConfigured ? <p>新账号自带本站模拟账户（初始资金 100 万）。</p> : null}
            {slowLine}
            <div className="approval-actions">
              <button className="ghost-button" type="button" onClick={backToForm} disabled={busy}>返回修改</button>
              <button className="primary-button accent" type="button" onClick={() => void register()} disabled={busy} autoFocus>
                {busy ? "正在创建…" : "创建并登录"}
              </button>
            </div>
          </>
        ) : mode === "register" && onRegister ? (
          <>
            <h2 id="cloud-login-title">注册 Sente</h2>
            <form onSubmit={submitRegistration} noValidate>
              <label className="credential-field">
                <span>账号</span>
                <input aria-label="账号" autoComplete="username" placeholder="3–32 位字母、数字、_ 或 -"
                  value={username} onChange={(event) => setUsername(event.target.value)} disabled={busy} autoFocus />
              </label>
              <label className="credential-field">
                <span>密码</span>
                <input aria-label="密码" type="password" autoComplete="new-password" placeholder={`至少 ${MIN_PASSWORD} 位`}
                  value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy} />
              </label>
              <label className="credential-field">
                <span>确认密码</span>
                <input aria-label="确认密码" type="password" autoComplete="new-password" placeholder="再输一次密码"
                  value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} disabled={busy} />
              </label>
              <label className="credential-field">
                <span>邀请码</span>
                <input aria-label="邀请码" placeholder="向管理员索取"
                  value={inviteCode} onChange={(event) => setInviteCode(event.target.value)} disabled={busy} />
              </label>
              {paperConfigured ? <p className="cloud-login-note">新账号自带本站模拟账户（初始资金 100 万）</p> : null}
              {error ? <p className="credential-warning" role="alert">{error}</p> : null}
              {slowLine}
              <div className="approval-actions">
                {!required ? <button className="ghost-button" type="button" onClick={close} disabled={busy}>取消</button> : null}
                <button className="primary-button accent" type="submit"
                  disabled={!username.trim() || !password || !confirmPassword || !inviteCode.trim() || busy}>
                  {busy ? "正在注册…" : "注册并登录"}
                </button>
              </div>
            </form>
            <p className="cloud-login-alt">
              已有账号？<button type="button" className="link-button" onClick={() => switchMode("login")} disabled={busy}>登录</button>
            </p>
          </>
        ) : (
          <>
            <h2 id="cloud-login-title">登录 Sente</h2>
            <p>本站账号和快期账号都在这里登录。</p>
            <form onSubmit={submit}>
              <label className="credential-field">
                <span>账号</span>
                <input
                  aria-label="账号"
                  autoComplete="username"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  disabled={busy}
                  autoFocus
                />
              </label>
              <label className="credential-field">
                <span>密码</span>
                <input
                  aria-label="密码"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  disabled={busy}
                />
              </label>
              {/* A TqKq account's first login needs the administrator's code (`tqkqLogin.inviteRequired`);
                  a typed code also lets an unknown name be offered “创建新账号？”. */}
              <label className="credential-field">
                <span>邀请码（快期账号首次登录填写）</span>
                <input
                  aria-label="邀请码"
                  placeholder="已登录过的账号留空"
                  value={inviteCode}
                  onChange={(event) => setInviteCode(event.target.value)}
                  disabled={busy}
                />
              </label>
              {error ? <p className="credential-warning" role="alert">{error}</p> : null}
              {slowLine}
              <div className="approval-actions">
                {!required ? <button className="ghost-button" type="button" onClick={close} disabled={busy}>取消</button> : null}
                <button className="primary-button accent" type="submit" disabled={!username.trim() || !password || busy}>
                  {busy ? "正在登录…" : "登录"}
                </button>
              </div>
            </form>
            {onRegister ? (
              <p className="cloud-login-alt">
                还没有账号？<button type="button" className="link-button" onClick={() => switchMode("register")} disabled={busy}>注册</button>
              </p>
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}
