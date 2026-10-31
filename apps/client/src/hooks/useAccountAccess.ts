import { createContext, useCallback, useMemo, useRef } from "react";
import type { AgentSkill, OwnerAuthStatus } from "../api";
import { skillBlockedLine } from "../components/futures/skillAccess";

/** One interaction policy for guest, test and member tiers. The Runtime remains
 * authoritative; this guard explains refusals before mounting costly features. */
export interface WorkspaceAccess {
  message?: string;
  reason: (capability: string) => string | undefined;
  require: (capability: string) => boolean;
  deny?: (message: string) => void;
}

export const AccountAccessContext = createContext<WorkspaceAccess | undefined>(undefined);

export function useAccountAccess({ guest, skills, status, onLogin, onNotice }: {
  guest: boolean; skills: AgentSkill[] | null; status?: OwnerAuthStatus;
  onLogin: () => void; onNotice: (message: string) => void;
}): WorkspaceAccess {
  const callbacks = useRef({ onLogin, onNotice });
  callbacks.current = { onLogin, onNotice };
  const reason = useCallback((capability: string) => {
    if (guest) return ["market-lookup", "news", "appearance"].includes(capability)
      ? undefined : "登录后可使用此功能";
    return skillBlockedLine(capability, skills, status?.features) ?? undefined;
  }, [guest, skills, status?.features]);
  const deny = useCallback((message: string) => { callbacks.current.onNotice(message); if (guest) callbacks.current.onLogin(); }, [guest]);
  const require = useCallback((capability: string) => {
    const message = reason(capability);
    if (!message) return true;
    deny(message);
    return false;
  }, [reason, deny]);
  return useMemo(() => ({ reason, require, deny }), [reason, require, deny]);
}
