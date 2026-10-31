import { useCallback, useMemo, useRef, useState } from "react";
import type { MobileMarketPreferences, PreferencesDocument } from "../../userPreferences";
import { useDisclosureSurface } from "../../uiSurfaces";
import type { ChartIndicatorPatch } from "../chartIndicatorEditor";
import type { SubPaneType } from "../chartIndicators";
import { normalizeMobileMarket, planMobileMarketChange, type MobileMarketChange } from "./mobileMarketPrefs";

const GUEST_KEY = "sente-mobile-market-v1";

function readGuest(): Partial<MobileMarketPreferences> {
  try {
    const saved = JSON.parse(window.localStorage.getItem(GUEST_KEY) ?? "null") as Partial<MobileMarketPreferences> | null;
    return saved && typeof saved === "object" ? saved : {};
  } catch { return {}; }
}

export interface MobileMarketHandle {
  market: MobileMarketPreferences;
  /** Shown at once; a member's account preference is written behind it (a visitor's stays on this device). */
  update: (patch: Partial<MobileMarketPreferences>) => void;
  sheetOpen: boolean;
  setSheetOpen: (open: boolean) => void;
  /** The `mobileMarket` UI command: throws the reason a change is refused. */
  apply: (change: MobileMarketChange) => void;
}

/**
 * 手机看盘 (2026-10-09): 报价 / 多图 / 单图 and their settings. A member's values live in the account preference
 * `ui.mobileMarket` (the tab reopens on the last view on any phone); a visitor's in this browser. A tap shows its
 * value before the write returns; a failed write falls back to the saved document. The 副图设置 sheet is the
 * `disclosure: mobile-subpanes` surface while the 看盘 tab shows.
 */
export function useMobileMarket({ guest, connected, document, update: updatePreferences, panes, updateChartIndicators, resolveSymbol, selectSymbol, available }: {
  guest: boolean;
  connected: boolean;
  document: Partial<MobileMarketPreferences> | undefined;
  update: (patch: Record<string, unknown>) => Promise<PreferencesDocument>;
  panes: readonly SubPaneType[];
  updateChartIndicators: ((patch: ChartIndicatorPatch) => void) | undefined;
  resolveSymbol: (symbol: string) => string | undefined;
  selectSymbol: (symbol: string) => void;
  /** The 看盘 tab of a phone window shows (the sheet surface exists only then). */
  available: boolean;
}): MobileMarketHandle {
  const [guestValue, setGuestValue] = useState<Partial<MobileMarketPreferences>>(() => guest ? readGuest() : {});
  const [pending, setPending] = useState<Partial<MobileMarketPreferences>>({});
  const market = useMemo(() => normalizeMobileMarket({ ...(guest ? guestValue : document), ...pending }), [guest, guestValue, document, pending]);
  const latest = useRef(market);
  latest.current = market;
  const update = useCallback((patch: Partial<MobileMarketPreferences>) => {
    if (guest) {
      setGuestValue(current => {
        const next = { ...current, ...patch };
        try { window.localStorage.setItem(GUEST_KEY, JSON.stringify(next)); } catch { /* This device only; it still shows. */ }
        return next;
      });
      return;
    }
    setPending(current => ({ ...current, ...patch }));
    if (!connected) return;
    const settle = () => setPending(current => {
      const next = { ...current };
      for (const key of Object.keys(patch) as (keyof MobileMarketPreferences)[]) if (next[key] === patch[key]) delete next[key];
      return next;
    });
    void updatePreferences({ ui: { mobileMarket: patch } }).then(settle, settle);
  }, [guest, connected, updatePreferences]);
  const [sheetOpen, setSheetOpen] = useDisclosureSurface("mobile-subpanes", { available });
  const apply = useCallback((change: MobileMarketChange) => {
    const plan = planMobileMarketChange(change, latest.current, panes, resolveSymbol);
    if (plan.subPaneList) {
      if (!updateChartIndicators) throw new Error("连接后可设置");
      updateChartIndicators({ subPaneList: plan.subPaneList });
    }
    if (plan.symbol) selectSymbol(plan.symbol);
    if (plan.market) update(plan.market);
    if (plan.sheet !== undefined) setSheetOpen(plan.sheet);
  }, [panes, resolveSymbol, updateChartIndicators, selectSymbol, update, setSheetOpen]);
  return { market, update, sheetOpen, setSheetOpen, apply };
}
