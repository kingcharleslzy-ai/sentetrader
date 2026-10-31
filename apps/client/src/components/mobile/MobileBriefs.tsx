import { ChevronLeft } from "lucide-react";
import type { InstrumentCatalogItem } from "../../api";
import { BriefSubscriptionEditor } from "../BriefSubscriptions";
import { SettingRow, SettingsSection } from "../SettingsPanel";
import { Switch } from "../settings/controls";

/**
 * 我的 → AI 简报推送 (2026-10-05, `mobilePage: briefs`): the push switch (the same
 * `showMarketBriefs` as 设置 → 提醒 · 生成简讯), then the subscribed products and the 添加品种 picker.
 */
export function MobileBriefs({ baseUrl, enabled, blocked, offline, onToggle, instruments, watchlist, onClose }: {
  baseUrl: string | undefined;
  enabled: boolean;
  /** Why the tier cannot have briefs (the switch is then off and locked). */
  blocked?: string;
  offline: boolean;
  onToggle: (value: boolean) => void;
  instruments: readonly InstrumentCatalogItem[];
  watchlist: readonly string[];
  onClose: () => void;
}) {
  return (
    <section className="fdm-overlay fdm-page fdm-briefs" aria-label="AI 简报推送" data-testid="mobile-briefs" data-agent-target="mobile-briefs">
      <header className="fdm-head"><button type="button" className="fdm-back" aria-label="返回我的" onClick={onClose}><ChevronLeft /></button><strong>AI 简报推送</strong></header>
      <main className="fdm-content">
        <SettingsSection title="推送">
          <SettingRow label="AI 简报推送" hint={blocked}>
            <Switch label="AI 简报推送" checked={enabled} disabled={offline} onChange={onToggle} />
          </SettingRow>
        </SettingsSection>
        <BriefSubscriptionEditor baseUrl={baseUrl} instruments={instruments} watchlist={watchlist} disabled={offline || Boolean(blocked)} />
      </main>
    </section>
  );
}
