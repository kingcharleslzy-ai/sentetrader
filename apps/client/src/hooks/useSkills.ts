import { useEffect, useRef, useState } from "react";
import type { RuntimeStatus } from "@ai-trader/ui-events";
import { getAgentSkills, type AgentSkill } from "../api";

/** While the window stays in front, how often the registry is read again: a tier or switch
 *  changed in 后台 reaches a window nobody switches away from (no event carries it). */
export const SKILLS_REFRESH_MS = 5 * 60_000;

/** The skill registry as the current user sees it (`GET /api/agent/skills`):
 *  what the skill-gated buttons read instead of the legacy feature switches.
 *  Refetches on (re)login, on runtime.status identity changes (version,
 *  account), when the window comes back to the front and every few minutes while
 *  it stays there — a tier or switch changed in 后台 (its own page) shows without reloading. A Runtime without
 *  the endpoint (pre-registry) leaves `skills` null — consumers then fall back
 *  to the feature view in the status payload. */
export function useSkills(baseUrl: string | undefined, runtime: RuntimeStatus | undefined) {
  const [skills, setSkills] = useState<AgentSkill[] | null>(null);
  const [focusKey, setFocusKey] = useState(0);
  const statusKey = runtime ? `${runtime.version}:${runtime.account?.id ?? ""}` : "";
  const identity = `${baseUrl ?? ""}|${statusKey}`;
  const loadedFor = useRef("");
  useEffect(() => {
    const again = () => { if (document.visibilityState === "visible") setFocusKey(value => value + 1); };
    window.addEventListener("focus", again);
    document.addEventListener("visibilitychange", again);
    const timer = window.setInterval(again, SKILLS_REFRESH_MS);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", again);
      document.removeEventListener("visibilitychange", again);
    };
  }, []);
  useEffect(() => {
    if (!baseUrl) { setSkills(null); return; }
    let active = true;
    getAgentSkills(baseUrl)
      .then(result => { if (active) { loadedFor.current = identity; setSkills(result.items); } })
      // A failed refetch for the same identity keeps what was read; a new identity starts empty.
      .catch(() => { if (active && loadedFor.current !== identity) setSkills(null); });
    return () => { active = false; };
  }, [baseUrl, identity, focusKey]);
  return { skills };
}

