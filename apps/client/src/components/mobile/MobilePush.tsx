import { Check, ChevronLeft, ChevronRight } from "lucide-react";
import type { PushController, PushSupportInfo } from "../../webPush";
import { openedOn } from "../notifyKinds";
import { Switch } from "../settings/controls";

/** Why this device has no switch: the card in place of 这台设备 (样稿第二块). */
function Unsupported({ support, fcm, onClose }: { support: PushSupportInfo; fcm: boolean; onClose: () => void }) {
  const android = support.reason === "android";
  const title = android ? "安卓手机收不到网页推送" : support.reason === "chrome-desktop" || fcm ? "这个浏览器收不到锁屏提醒"
    : support.reason === "desktop-app" ? "桌面版不另开锁屏提醒" : support.reason === "ios-old" ? "这台设备的系统太旧" : "这个浏览器收不到锁屏提醒";
  const text = android ? "安卓的网页推送要走谷歌的推送服务，国内连不上，多数国产手机也没装。"
    : support.reason === "chrome-desktop" || fcm ? "Chrome 走谷歌的推送服务，国内连不上；请换 Edge 或 Safari 打开 Sente。"
    : support.reason === "desktop-app" ? "Sente 桌面版开着时照旧弹系统通知，免得同一件事弹两次。"
    : support.reason === "ios-old" ? "iPhone 从 iOS 16.4 起才能收网页的锁屏提醒，升级系统后再来打开。"
    : "换 Safari、Edge 或 Firefox 打开 Sente 后再来打开。";
  return <section className="fdm-push-card" aria-label={title}>
    <strong>{title}</strong>
    <p>{text}</p>
    <span className="fdm-push-line"><Check aria-hidden="true" />开着 Sente 时，提醒照常在页面里弹出</span>
    <button type="button" className="fdm-push-later" onClick={onClose}>知道了</button>
  </section>;
}

/**
 * 我的 → 提醒 → 锁屏提醒 (2026-10-06, `mobilePage: push`): this device's switch and 发一条试试 — or, where
 * the browser cannot take push, why (先放到主屏幕 / 安卓与 Chrome 收不到) — and the devices that have it
 * on. Which kinds remind and 显示金额和手数 are 设置 → 提醒's (2026-10-15), linked from here.
 */
export function MobilePush({ push, offline, onOpenSettings, onOpenInstall, onClose }: {
  push: PushController;
  offline: boolean;
  /** 提醒哪些事 / 显示金额和手数: 设置 → 提醒. */
  onOpenSettings: () => void;
  onOpenInstall: () => void;
  onClose: () => void;
}) {
  const { support, status, thisDevice, busy, fcm } = push;
  const configured = status?.configured === true;
  const devices = status?.devices ?? [];
  const subscribed = Boolean(thisDevice);
  return (
    <section className="fdm-overlay fdm-page fdm-push" aria-label="锁屏提醒" data-testid="mobile-push" data-agent-target="mobile-push"
      data-push-support={fcm ? "unsupported" : support.support}>
      <header className="fdm-head"><button type="button" className="fdm-back" aria-label="返回我的" onClick={onClose}><ChevronLeft /></button><strong>锁屏提醒</strong></header>
      <main className="fdm-content">
        {support.support === "needs-install" ? <section className="fdm-push-card" aria-label="先把 Sente 放到主屏幕">
          <strong>先把 Sente 放到主屏幕</strong>
          <p>{support.device} 只给放在主屏幕上的网页发锁屏提醒。放好后从桌面图标打开，再回这里点打开。</p>
          <button type="button" className="fdm-push-go" onClick={onOpenInstall}>教我放到主屏幕</button>
        </section> : support.support === "unsupported" || fcm ? <Unsupported support={support} fcm={fcm} onClose={onClose} />
          : <section className="fdm-me-group" aria-label="这台设备">
            <h3>这台设备</h3>
            <div className="fdm-me-row">
              <span>这台 {support.device} 收锁屏提醒
                <small>{!configured && status ? "服务器还没开通锁屏提醒" : subscribed ? `${support.label} · ${openedOn(thisDevice?.createdAt)}` : "Sente 没开着也能在锁屏收到"}</small></span>
              <Switch label={`这台 ${support.device} 收锁屏提醒`} checked={subscribed} disabled={offline || busy || !configured}
                onChange={value => void (value ? push.enable() : push.disable())} />
            </div>
            <div className="fdm-me-row">
              <span>发一条试试</span>
              <button type="button" className="fdm-push-act" disabled={!subscribed || busy || offline} onClick={() => void push.test(push.deviceId)}>发送</button>
            </div>
          </section>}
        {push.notice ? <p className="fdm-push-notice" role="status">{push.notice}</p> : null}
        <section className="fdm-me-group" aria-label="提醒哪些事">
          <button type="button" className="fdm-me-row" onClick={onOpenSettings}>
            <span>提醒哪些事<small>每类一个开关，锁屏上显示金额和手数也在这里</small></span>
            <span className="fdm-me-value">提醒设置<ChevronRight aria-hidden="true" /></span>
          </button>
        </section>
        <section className="fdm-me-group" aria-label="已开锁屏提醒的设备">
          <h3>已开锁屏提醒的设备</h3>
          {devices.length ? devices.map(device => <div className="fdm-me-row" key={device.deviceId}>
            <span>{device.platform}<small>{device.deviceId === push.deviceId ? "这台" : openedOn(device.createdAt)}</small></span>
            {device.deviceId === push.deviceId
              ? device.lastError ? <span className="fdm-push-status" data-tone="warning">没送达</span> : <span className="fdm-push-status">正常</span>
              : <button type="button" className="fdm-push-act" data-tone="danger" disabled={busy || offline}
                aria-label={`移除 ${device.platform}`} onClick={() => void push.remove(device.deviceId)}>移除</button>}
          </div>) : <div className="fdm-me-row"><span>还没有设备开启<small>在要收提醒的设备上打开</small></span></div>}
        </section>
      </main>
    </section>
  );
}
