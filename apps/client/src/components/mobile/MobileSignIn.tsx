import { LogIn } from "lucide-react";

/**
 * 登录后使用 (手机版游客, 2026-10-04): a visitor's AI / 通知 / 我的 tab — one line on what the tab
 * holds once signed in, and 登录, which opens the unified login dialog (new users enter an invite code).
 */
export function MobileSignIn({ title, text, onLogin }: { title: string; text: string; onLogin: () => void }) {
  return (
    <section className="fdm-page" aria-label={title} data-testid="mobile-signin" data-agent-target="mobile-signin">
      <header className="fdm-head"><strong>{title}</strong></header>
      <main className="fdm-content">
        <div className="fdm-empty fdm-signin">
          <LogIn aria-hidden="true" />
          <strong>登录后使用</strong>
          <span>{text}</span>
          <button type="button" className="fdm-signin-button" onClick={onLogin}>登录</button>
          <small>新用户填邀请码即可创建</small>
        </div>
      </main>
    </section>
  );
}
