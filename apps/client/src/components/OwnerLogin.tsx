import { useEffect, useState } from "react";
import {
  authenticateTqkq, getOwnerAuthStatus, registerOwnerAccount, type OwnerCredentialBindings,
} from "../api";
import { CloudLoginSheet } from "./CloudLoginSheet";

const LAST_USERNAME_KEY = "ai-trader:last-username";
/** Written by clients before the one login form; read once as a fallback. */
const LEGACY_TQKQ_USERNAME_KEY = "ai-trader:last-tqkq-username";
const PAPER_OPENED = "已开好本站模拟账户（初始资金 100 万）";

/** What the toast says right after a TqKq login: the account it lands on may still be
 *  assembling (the header keeps reporting until it connects). */
const ASSEMBLY_MESSAGES: Record<string, string> = {
  connected: "已登录，已连接模拟账户",
  connecting: "已登录，正在连接模拟盘…",
  queued: "已登录，服务器在线人数已满，排队中，稍后自动重试",
};

/** The toast after a password login (the administrator or a site account). */
function bindingMessage(bindings: OwnerCredentialBindings) {
  const tqkqBound = bindings.tqkqConfigured ? "快期账户" : "演示行情";
  const model = bindings.modelCredentialConfigured
    ? bindings.modelProvider === "moonshot" ? "Kimi K3.1 Pro" : bindings.modelProvider === "deepseek" ? "DeepSeek" : "AI 模型"
    : "AI 模型";
  return `已登录，已载入${tqkqBound}和${model}绑定`;
}

interface Props {
  baseUrl: string;
  open: boolean;
  required: boolean;
  onClose: () => void;
  /** A new login or registration reconnects the session under the new identity. */
  onAuthenticated: (message: string) => void;
}

/** The one login form (owner 2026-10-04): every account — the administrator, site accounts
 *  and TqKq accounts — logs in through `POST /api/auth/tqkq`; an unknown new name is offered
 *  a site account after the person confirms, and 注册 opens our own sign-up form (2026-10-05). */
export function OwnerLogin({ baseUrl, open, required, onClose, onAuthenticated }: Props) {
  // Only this browser's own last login is remembered: the public login box never suggests anyone else's name.
  const lastUsername = typeof window === "undefined" ? ""
    : window.localStorage.getItem(LAST_USERNAME_KEY) || window.localStorage.getItem(LEGACY_TQKQ_USERNAME_KEY) || "";
  const [paperConfigured, setPaperConfigured] = useState(false);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    getOwnerAuthStatus(baseUrl)
      .then(status => { if (!cancelled) setPaperConfigured(status.paperConfigured === true); })
      .catch(() => { if (!cancelled) setPaperConfigured(false); });
    return () => { cancelled = true; };
  }, [baseUrl, open]);
  const remember = (username: string) => {
    try { window.localStorage.setItem(LAST_USERNAME_KEY, username); } catch { /* Remembered only. */ }
  };
  return (
    <CloudLoginSheet
      open={open}
      required={required}
      onClose={onClose}
      defaultUsername={lastUsername}
      paperConfigured={paperConfigured}
      onLogin={async (username, password, inviteCode) => {
        const result = await authenticateTqkq(baseUrl, username, password, inviteCode);
        remember(username);
        if (result.status === "authenticated") {
          onAuthenticated(bindingMessage(result.bindings));
        } else if (result.firstLogin && paperConfigured) {
          // A new TqKq user starts on the site simulation account.
          onAuthenticated(`已登录，${PAPER_OPENED}`);
        } else {
          onAuthenticated(ASSEMBLY_MESSAGES[result.assembly] ?? ASSEMBLY_MESSAGES.connected);
        }
      }}
      onRegister={async (username, password, inviteCode) => {
        await registerOwnerAccount(baseUrl, username, password, inviteCode);
        remember(username);
        onAuthenticated(paperConfigured ? `注册成功，${PAPER_OPENED}` : "注册成功，已登录");
      }}
    />
  );
}
