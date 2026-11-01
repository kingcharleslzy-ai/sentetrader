import { ChevronLeft, ChevronRight } from "lucide-react";
import type { ReactNode } from "react";
import { CLIENT_BUILD, shortCommit } from "../../clientBuild";
import { useWebUpdateCheck } from "../WebUpdate";
import type { MobilePage } from "./MobileWorkspace";

function Row({ label, value, onOpen }: { label: string; value?: ReactNode; onOpen: () => void }) {
  return <button type="button" className="fdm-me-row" onClick={onOpen}>
    <span>{label}</span><span className="fdm-me-value">{value}<ChevronRight aria-hidden="true" /></span>
  </button>;
}

/**
 * 我的 (手机版第二期, 2026-10-04): who is signed in and on which account, then three groups — 账户
 * (持仓与委托, 复盘摘要, 交易账户), 提醒 (提醒设置 = 设置 → 提醒 with every kind's switch since 2026-10-15, 到价提醒,
 * 锁屏提醒 on the web, AI 简报推送) and 设置 (添加到手机桌面 on the web, 外观与字号, AI 与记忆, 帮助). Each row opens the
 * existing panel or settings page in its phone form (AI 简报推送 its own page: the switch and the subscribed
 * products). The foot names this build and, on the web, offers
 * 检查更新 (a newer deployed build reloads the page). 方案 B (2026-11-01): opened from the avatar on 今天, back returns there.
 * 对齐电脑版 (2026-11-01): 持仓与委托 and 复盘 moved to 今天's 持仓 tile (复盘 from the positions page's head); 设置 adds
 * 下单与停止, 语音, 费率 and 反馈 — the desktop's settings pages, full screen.
 */
export function MobileMe({ userName, accountLabel, equity, alertCount, appearance, onOpenPage, onOpenSettings, onOpenAlerts,
  notify, onOpenReminderSettings, installed, pushValue, onBack }: {
  userName: string;
  accountLabel: string;
  equity: string;
  alertCount: number;
  /** 外观与字号's summary, e.g. 深色 · 标准. */
  appearance: string;
  onOpenPage: (page: MobilePage) => void;
  /** 设置 at one of its pages, full screen. */
  onOpenSettings: (page: "trade" | "voice" | "trade-advanced" | "feedback") => void;
  /** 到价提醒: the 看盘 tab with the alerts popover open. */
  onOpenAlerts: () => void;
  notify: {
    briefs: boolean;
    /** Subscribed brief products (undefined until read). */
    briefCount?: number;
  };
  /** 提醒设置: 设置 → 提醒 (every reminder kind's switch). */
  onOpenReminderSettings: () => void;
  /** 添加到手机桌面 (web only): whether this page runs from the home screen. */
  installed?: boolean;
  /** 锁屏提醒 (web only, 2026-10-06): 已开 / 未开 / 先装到桌面 / 不支持. */
  pushValue?: string;
  /** Back to 今天. */
  onBack?: () => void;
}) {
  const update = useWebUpdateCheck();
  return (
    <section className="fdm-page fdm-me" aria-label="我的" data-testid="mobile-me" data-agent-target="mobile-me">
      {onBack ? <header className="fdm-head"><button type="button" className="fdm-back" aria-label="返回今天" onClick={onBack}><ChevronLeft /></button><strong>我的</strong></header> : null}
      <main className="fdm-content">
        <header className="fdm-me-profile">
          <span className="fdm-avatar" aria-hidden="true">{[...(userName || "我")][0]}</span>
          <span><strong>{userName || "我的"}</strong><small>{accountLabel} · 权益 {equity}</small></span>
        </header>
        <section className="fdm-me-group" aria-label="账户">
          <h3>账户</h3>
          <Row label="交易账户" value={accountLabel} onOpen={() => onOpenPage("accounts")} />
        </section>
        <section className="fdm-me-group" aria-label="提醒">
          <h3>提醒</h3>
          <Row label="提醒设置" value="每类一个开关" onOpen={onOpenReminderSettings} />
          <Row label="到价提醒" value={`${alertCount} 条`} onOpen={onOpenAlerts} />
          {CLIENT_BUILD.platform === "web" && pushValue ? <Row label="锁屏提醒" value={pushValue} onOpen={() => onOpenPage("push")} /> : null}
          <Row label="AI 简报推送" value={notify.briefs ? notify.briefCount === undefined ? "" : `已订阅 ${notify.briefCount} 个` : "已关闭"}
            onOpen={() => onOpenPage("briefs")} />
        </section>
        <section className="fdm-me-group" aria-label="设置">
          <h3>设置</h3>
          {CLIENT_BUILD.platform === "web" ? <Row label="添加到手机桌面" value={installed ? "已在桌面" : "未添加"} onOpen={() => onOpenPage("install")} /> : null}
          <Row label="下单与停止" onOpen={() => onOpenSettings("trade")} />
          <Row label="语音" onOpen={() => onOpenSettings("voice")} />
          <Row label="外观与字号" value={appearance} onOpen={() => onOpenPage("appearance")} />
          <Row label="AI 与记忆" onOpen={() => onOpenPage("memory")} />
          <Row label="费率" onOpen={() => onOpenSettings("trade-advanced")} />
          <Row label="反馈" onOpen={() => onOpenSettings("feedback")} />
          <Row label="帮助" onOpen={() => onOpenPage("help")} />
        </section>
        <p className="fdm-me-foot">回放、快捷键、看板列设置和各项高级设置请在电脑上使用。</p>
        <p className="fdm-me-version" data-testid="mobile-me-version">
          <span>版本 {CLIENT_BUILD.version} · {shortCommit()}</span>
          {CLIENT_BUILD.platform === "web" ? <>
            <span aria-hidden="true">·</span>
            <button type="button" disabled={update.busy} onClick={update.check}>{update.label}</button>
            {update.notice ? <span role="status">{update.notice}</span> : null}
          </> : null}
        </p>
      </main>
    </section>
  );
}
