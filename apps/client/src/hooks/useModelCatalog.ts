import { useCallback, useEffect, useState } from "react";
import { getModelCatalog, setModelPreference, type ModelCatalog } from "../api";

export function useModelCatalog(baseUrl: string | undefined, reloadKey: number) {
  const [catalog, setCatalog] = useState<ModelCatalog>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    if (!baseUrl) {
      setCatalog(undefined);
      return;
    }
    let active = true;
    getModelCatalog(baseUrl)
      .then((value) => {
        if (active) {
          setCatalog(value);
          setError(undefined);
        }
      })
      .catch(() => {
        if (active) setError("模型目录暂时不可用");
      });
    return () => {
      active = false;
    };
  }, [baseUrl, reloadKey]);

  const select = useCallback(async (model: string) => {
    if (!baseUrl || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      setCatalog(await setModelPreference(baseUrl, model));
    } catch {
      setError("切换未生效，请稍后再试");
    } finally {
      setBusy(false);
    }
  }, [baseUrl, busy]);

  return { catalog, busy, error, select };
}
