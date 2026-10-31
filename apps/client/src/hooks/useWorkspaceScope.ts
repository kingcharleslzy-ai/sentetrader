import { useEffect, useState } from "react";
import { requestJson } from "../api";
export function useWorkspaceScope(rootUrl: string, provided?: string) {
  const [scope, setScope] = useState<{ url: string; value: string }>();
  useEffect(() => { if (provided || !rootUrl) return; let cancelled = false; void requestJson<{scope: string}>(`${rootUrl}/api/workspace`).then(value => { if (!cancelled) setScope({url: rootUrl, value: value.scope}); }).catch(() => {}); return () => { cancelled = true; }; }, [rootUrl, provided]);
  return provided || (scope?.url === rootUrl ? scope.value : undefined);
}
