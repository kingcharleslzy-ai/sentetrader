import { ArrowDown, ArrowUp, Ellipsis, EllipsisVertical, Menu, Share, SquarePlus, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { BrandMark } from "../BrandMark";
import {
  currentInstallDetection, installGuideContent, installedThisVisit, promptInstall, readInstallRecord, shouldAutoOpenInstall,
  subscribeInstallPrompt, updateInstallRecord, type AutoOpenInput, type GuideGlyph, type InstallDetection,
} from "./installEnvironment";

const GLYPHS: Record<GuideGlyph, typeof Share> = { more: Ellipsis, share: Share, plus: SquarePlus, menu: Menu, vmore: EllipsisVertical };
const DISPLAY_QUERY = "(display-mode: standalone), (display-mode: fullscreen)";

function subscribeDetection(listener: () => void) {
  const stop = subscribeInstallPrompt(listener);
  let query: MediaQueryList | undefined;
  try { query = window.matchMedia?.(DISPLAY_QUERY); } catch { query = undefined; }
  query?.addEventListener?.("change", listener);
  return () => { stop(); query?.removeEventListener?.("change", listener); };
}

/** This page's install environment, following the Android prompt and the display mode live. */
export function useInstallDetection(): InstallDetection {
  const key = useSyncExternalStore(subscribeDetection, () => JSON.stringify(currentInstallDetection()), () => JSON.stringify({ environment: "desktop" }));
  return useMemo(() => JSON.parse(key) as InstallDetection, [key]);
}

/** The address to open elsewhere: the web app's root, not the page's current state. */
const appAddress = () => `${window.location.origin}${import.meta.env.BASE_URL || "/"}`;

function CopyLink({ label, className }: { label: string; className: string }) {
  const [state, setState] = useState<"idle" | "copied" | "manual">("idle");
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => { if (state === "manual") field.current?.select(); }, [state]);
  const copy = () => {
    const done = () => setState("copied");
    try {
      const write = navigator.clipboard?.writeText(appAddress());
      if (!write) { setState("manual"); return; }
      write.then(done, () => setState("manual"));
    } catch { setState("manual"); }
  };
  return <>
    <button type="button" className={className} onClick={copy}>{state === "copied" ? "已复制" : label}</button>
    {state === "copied" ? <p className="fdm-install-copied" role="status">链接已复制，到浏览器地址栏粘贴打开。</p> : null}
    {state === "manual" ? <span className="fdm-install-manual">
      <input ref={field} readOnly value={appAddress()} aria-label="Sente 网址" onFocus={event => event.currentTarget.select()} />
      <small role="status">没能自动复制，长按上面的网址复制。</small>
    </span> : null}
  </>;
}

/**
 * 安装到桌面 (2026-10-06, `mobilePage: install`): what this browser needs to put Sente on the home
 * screen — a bottom card with numbered steps and an arrow at the browser's own button, the one
 * 安装到桌面 button where Android offers its install dialog, or (inside 微信 / QQ / 钉钉 / 支付宝) a
 * full-screen mask that sends the reader to a real browser with 复制链接 as the fallback.
 */
export function InstallGuide({ detection, onClose, onOpenPush }: { detection: InstallDetection; onClose: () => void;
  /** 已在桌面 (2026-10-06): the 锁屏提醒 page, offered where this device can take push and has not turned it on. */
  onOpenPush?: () => void }) {
  const content = installGuideContent(detection);
  const [installing, setInstalling] = useState(false);
  const install = async () => {
    setInstalling(true);
    await promptInstall();
    setInstalling(false);
    onClose();
  };
  const common = { "aria-label": "安装引导", "data-testid": "mobile-install", "data-agent-target": "mobile-install", "data-install-environment": detection.environment } as const;
  if (content.layout === "mask") {
    return (
      <section className="fdm-install-mask" role="dialog" {...common}>
        <svg className="fdm-install-curve" viewBox="0 0 70 60" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M8 56C30 50 48 36 56 8" /><path d="M47 14l9-7 4 11" />
        </svg>
        <strong>{content.title.split("\n").map((line, index) => <span key={index}>{line}</span>)}</strong>
        <p>{content.subtitle}</p>
        <div className="fdm-install-mask-actions">
          {content.copy ? <CopyLink label={content.copy} className="fdm-install-mask-copy" /> : null}
        </div>
        <button type="button" className="fdm-install-later" onClick={onClose}>以后再说</button>
      </section>
    );
  }
  return (
    <div className="fdm-sheet-backdrop" onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="fdm-sheet fdm-install" role="dialog" {...common}>
        <i className="fdm-sheet-grip" aria-hidden="true" />
        <header className="fdm-sheet-head fdm-install-head">
          <BrandMark size={40} />
          <span><strong>{content.title}</strong><small>{content.subtitle}</small></span>
          <button type="button" aria-label="关闭安装引导" onClick={onClose}><X aria-hidden="true" /></button>
        </header>
        {content.steps.length ? <ol className="fdm-install-steps">
          {content.steps.map((step, index) => (
            <li key={index}><span>{step.map((part, at) => typeof part === "string" ? part : (() => {
              const Glyph = GLYPHS[part.glyph];
              return <span className="fdm-install-glyph" key={at}><Glyph aria-hidden="true" /></span>;
            })())}</span></li>
          ))}
        </ol> : null}
        <footer className="fdm-install-foot">
          {content.layout === "done"
            ? onOpenPush ? <>
              <button type="button" className="fdm-install-later" onClick={onClose}>知道了</button>
              <button type="button" className="fdm-install-go" onClick={onOpenPush}>打开锁屏提醒</button>
            </> : <button type="button" className="fdm-install-go" onClick={onClose}>知道了</button>
            : <>
              <button type="button" className="fdm-install-later" onClick={onClose}>以后再说</button>
              {content.layout === "prompt" ? <button type="button" className="fdm-install-go" disabled={installing} onClick={() => void install()}>安装到桌面</button>
                : content.copy ? <CopyLink label={content.copy} className="fdm-install-copy" /> : null}
            </>}
        </footer>
      </section>
      {content.pointer ? <span className="fdm-install-pointer" data-pointer={content.pointer} aria-hidden="true">
        {content.pointer.startsWith("top") ? <ArrowUp /> : <ArrowDown />}
      </span> : null}
    </div>
  );
}

let openCounted = false;
let shownThisVisit = false;

/**
 * The guide's own timing: this page load counts as one open of this browser, an order confirmed or
 * an AI reply here marks it engaged, and the guide opens by itself once `shouldAutoOpenInstall`
 * allows (second open on, or after the first engagement; never mid-hold, over a page or beside an
 * unconfirmed order card). Opening it by hand settles this visit too (#522). Each opened guide keeps
 * where it came from; only closing one that opened by itself counts as 以后再说.
 */
export function useInstallAutoOpen({ enabled, detection, signedIn, engaged, holdingToTalk, pageOpen, pendingOrder, installPageOpen, open }: {
  /** Phone layout on the web (never the desktop app). */
  enabled: boolean;
  detection: InstallDetection;
  signedIn: boolean;
  engaged: boolean;
  holdingToTalk: boolean;
  pageOpen: boolean;
  pendingOrder: boolean;
  installPageOpen: boolean;
  open: () => void;
}) {
  /** The automatic open asked for and not yet shown. */
  const autoPending = useRef(false);
  /** Where the guide on screen came from; null while it is closed. */
  const source = useRef<"auto" | "manual" | null>(null);
  const wasOpen = useRef(false);
  const openRef = useRef(open);
  openRef.current = open;
  useEffect(() => {
    if (!enabled || openCounted) return;
    openCounted = true;
    updateInstallRecord(record => ({ ...record, opens: record.opens + 1 }));
  }, [enabled]);
  useEffect(() => {
    if (enabled && engaged) updateInstallRecord(record => record.engaged ? record : { ...record, engaged: true });
  }, [enabled, engaged]);
  // Opening and closing first, so the automatic open below never sees a guide that just closed as its own.
  useEffect(() => {
    if (installPageOpen && !wasOpen.current) {
      source.current = autoPending.current ? "auto" : "manual";
      autoPending.current = false;
      shownThisVisit = true;
    } else if (!installPageOpen && wasOpen.current) {
      if (source.current === "auto" && !installedThisVisit()) {
        updateInstallRecord(record => ({ ...record, dismissals: record.dismissals + 1, lastDismissedAt: Date.now() }));
      }
      source.current = null;
    }
    wasOpen.current = installPageOpen;
  }, [installPageOpen]);
  useEffect(() => {
    const input: AutoOpenInput = { record: readInstallRecord(), now: Date.now(), environment: detection.environment, mobileLayout: enabled,
      tauri: false, signedIn, shownThisVisit, holdingToTalk, pageOpen: pageOpen || installPageOpen, pendingOrder };
    if (!shouldAutoOpenInstall(input)) return;
    shownThisVisit = true;
    autoPending.current = true;
    openRef.current();
  }, [enabled, detection.environment, signedIn, engaged, holdingToTalk, pageOpen, installPageOpen, pendingOrder]);
}
