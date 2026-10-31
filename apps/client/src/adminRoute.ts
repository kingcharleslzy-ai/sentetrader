/** The standalone 后台管理 page (owner 2026-10-01: qilinshuzhi.com/trader/admin): the same web
 *  build serves it under the app's base path while the base itself keeps the trading workspace —
 *  the path, not the build, decides which app mounts. */
export function isAdminPath(pathname: string, base: string = import.meta.env.BASE_URL): boolean {
  return pathname.replace(/\/+$/, "") === `${base.replace(/\/+$/, "")}/admin`;
}
