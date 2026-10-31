/*
 * Sente web service worker (2026-10-06). Network only: it caches no page and no asset, so a new
 * release is never hidden behind an old shell (why the precaching worker was retired). It exists
 * for three things: Android Chrome offers "install" only to a page whose worker handles fetch, an
 * open page that loses the network gets a short offline page instead of the browser's error, and
 * 锁屏提醒 (Web Push, 2026-10-06) arrives here: `push` shows the notification the Runtime sent
 * (title, body, tag, deep link) and `notificationclick` focuses an open Sente window — handing it
 * the link — or opens one at it. Only same-origin GET navigations are handled; everything else
 * goes straight to the network untouched. Built by vite-plugin-pwa (injectManifest without a
 * manifest) to dist/sw.js at the app's base path.
 */

const OFFLINE_PAGE = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Sente · 网络断开</title>
<style>
  :root { --bg: #f4f4f0; --ink: #111310; --muted: #5c615b; --primary-bg: #2f5bff; --on-primary: #ffffff; color-scheme: light; }
  @media (prefers-color-scheme: dark) { :root { --bg: #060708; --ink: #eceee8; --muted: #9a9f98; --primary-bg: #3866f8; color-scheme: dark; } }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--ink);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif; }
  main { display: grid; justify-items: center; gap: 12px; padding: 24px 16px; text-align: center; }
  strong { font-size: 18px; }
  p { margin: 0; color: var(--muted); }
  button { min-height: 44px; padding: 0 24px; border: 0; border-radius: 22px; background: var(--primary-bg); color: var(--on-primary); font: inherit; font-weight: 600; }
</style>
</head>
<body>
<main>
  <svg width="48" height="48" viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="14" fill="#0b0d10"/><circle cx="27" cy="38" r="13" fill="#eceee8"/><circle cx="46" cy="19" r="7" fill="#3d6bff"/></svg>
  <strong>网络断开，连上后自动恢复</strong>
  <p>Sente 需要联网读取行情和账户。</p>
  <button type="button" onclick="location.reload()">重试</button>
</main>
<script>addEventListener("online", function () { location.reload(); });</script>
</body>
</html>`;

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    // A leftover precaching worker's caches (workbox names carry the scope) and anything named ai-trader.
    const scope = self.registration.scope;
    const names = await caches.keys();
    await Promise.all(names.filter((name) => name.includes(scope) || /ai-trader/i.test(name)).map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" || request.mode !== "navigate" || new URL(request.url).origin !== self.location.origin) return;
  event.respondWith(fetch(request).catch(() => new Response(OFFLINE_PAGE, {
    status: 503,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  })));
});

self.addEventListener("push", (event) => {
  let data;
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data ? event.data.text() : "" }; }
  const scope = self.registration.scope;
  const icon = new URL("icons/icon-192.png", scope).href;
  // Safari requires every push to show a notification; a tag replaces the previous one of its kind.
  event.waitUntil(self.registration.showNotification(String(data.title || "Sente"), {
    body: String(data.body || ""), icon, badge: icon, tag: data.tag ? String(data.tag) : undefined, renotify: Boolean(data.tag),
    data: { url: new URL(String(data.url || ""), scope).href },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const scope = self.registration.scope;
  const target = String((event.notification.data && event.notification.data.url) || scope);
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find((client) => client.url.startsWith(scope));
    if (open) {
      // Admin alerts open the standalone console, which does not consume workspace deep links.
      if (new URL(target).pathname !== new URL(open.url).pathname || new URL(target).pathname.endsWith("/admin")) {
        await open.navigate(target);
        await open.focus();
        return;
      }
      await open.focus();
      open.postMessage({ type: "sente:open", url: target });
      return;
    }
    await self.clients.openWindow(target);
  })());
});
