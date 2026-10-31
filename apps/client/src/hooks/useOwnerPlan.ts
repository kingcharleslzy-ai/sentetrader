import { useCallback, useEffect, useState } from "react";
import { getOwnerAuthStatus, type OwnerAuthStatus } from "../api";

/**
 * M2: the signed-in cloud user's tier (`GET /api/auth/owner/status`). One read
 * per Runtime; `reload()` re-reads it after the owner changed something. A local
 * single-user Runtime answers 404 — `status` stays undefined and no feature is
 * grayed (local users are unlimited).
 */
export function useOwnerPlan(baseUrl: string | undefined) {
  const [status, setStatus] = useState<OwnerAuthStatus>();
  const [reloadKey, setReloadKey] = useState(0);
  useEffect(() => {
    if (!baseUrl) { setStatus(undefined); return; }
    let active = true;
    // The first read reuses the one the connection just made; reload() always asks again.
    getOwnerAuthStatus(baseUrl, { maxAgeMs: reloadKey ? 0 : 5000 })
      .then(value => { if (active && value.authenticated) setStatus(value); })
      .catch(() => { /* not an owner-password Runtime (or briefly offline): nothing to gray */ });
    return () => { active = false; };
  }, [baseUrl, reloadKey]);
  const reload = useCallback(() => setReloadKey(key => key + 1), []);
  return { status, reload };
}
