import { useEffect, useState } from "react";
import { getOwnerAuthStatus, signOutOwner, type OwnerAuthStatus } from "./api";
import { useClientCompat } from "./clientBuild";
import { configuredRuntimeUrl, OWNER_RUNTIME_URL } from "./runtimeConnection";
import { OwnerLogin } from "./components/OwnerLogin";
import { AdminConsole } from "./components/futures/AdminConsole";
import type { AdminSection } from "./components/futures/admin/shared";
import { Button } from "./components/settings/controls";
import "./AdminApp.css";

/**
 * The standalone 后台管理 page (owner 2026-09-30): qilinshuzhi.com/trader/admin, outside
 * the trading workspace. A visitor who is not signed in gets the full-page login
 * sheet; a signed-in non-owner is told the console is owner-only; the owner gets the
 * console full-page, its sidebar carrying the account, Runtime version and page links.
 * It registers no UI-control window and joins no session stream — it is not a
 * trading workspace window.
 */
export function AdminApp() {
  // The web build carries the Runtime URL; the constant covers a dev server without it.
  const baseUrl = configuredRuntimeUrl("cloud") || OWNER_RUNTIME_URL;
  const [status, setStatus] = useState<OwnerAuthStatus | undefined>();
  const [failed, setFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [section, setSection] = useState<AdminSection>(() =>
    new URLSearchParams(window.location.search).get("section") === "servers" ? "servers" : "overview");
  // `<version>+<short sha>` from the Runtime's response headers, once any request answered.
  const runtime = useClientCompat().runtime;

  useEffect(() => { document.title = "Sente 后台管理"; }, []);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    getOwnerAuthStatus(baseUrl)
      .then(result => { if (!cancelled) setStatus(result); })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [baseUrl, reload]);

  const refresh = () => setReload(count => count + 1);
  const signOut = async () => {
    await signOutOwner(baseUrl).catch(() => undefined);
    refresh();
  };

  const openTrader = <a className="admin-open-trader" href="/trader/">打开交易页</a>;
  const signedIn = status?.authenticated === true;
  const owner = signedIn && status?.role === "owner";

  return (
    <div className="admin-page">
      {!signedIn ? (
        <header className="admin-topbar">
          <strong className="admin-brand">Sente 后台管理</strong>
          <div className="admin-topbar-actions">{openTrader}</div>
        </header>
      ) : null}
      {failed ? (
        <main className="admin-gate">
          <section className="admin-gate-card">
            <h1>连接失败</h1>
            <p>读取账号状态失败，请刷新重试。</p>
            <div className="admin-gate-actions"><Button onClick={refresh}>重试</Button></div>
          </section>
        </main>
      ) : !status ? (
        <main className="admin-gate"><p>读取中…</p></main>
      ) : !signedIn ? (
        <OwnerLogin baseUrl={baseUrl} open required onClose={() => undefined} onAuthenticated={refresh} />
      ) : !owner ? (
        <main className="admin-gate">
          <section className="admin-gate-card">
            <h1>后台只对管理员开放</h1>
            <p>当前账号：{status.username ?? "（未知）"}</p>
            <div className="admin-gate-actions">
              <Button onClick={() => void signOut()}>退出登录</Button>
              {openTrader}
            </div>
          </section>
        </main>
      ) : (
        <main className="admin-body">
          <AdminConsole baseUrl={baseUrl} section={section} onSectionChange={setSection} footer={<>
            <div className="admin-account-line">
              <span className="admin-account">{status.username}</span>
              <span className="admin-account-role">· 管理员</span>
            </div>
            {runtime ? <span className="admin-runtime">Runtime {runtime.replace("+", " · ")}</span> : null}
            <div className="admin-account-actions">
              {openTrader}
              <Button onClick={() => void signOut()}>退出登录</Button>
            </div>
          </>} />
        </main>
      )}
    </div>
  );
}
