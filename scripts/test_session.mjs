#!/usr/bin/env node
// Online acceptance with the TqKq test account, without agents ever typing its password
// (owner 2026-10-12). The owner logs in once in a dedicated browser profile; agents then reuse
// that saved session for API calls and UI scripts until it expires (about 30 days).
//
//   node scripts/test_session.mjs login            open a browser window; the owner logs in, then closes it
//   node scripts/test_session.mjs status           who the saved session is (headless)
//   node scripts/test_session.mjs call GET /api/runtime/status
//   node scripts/test_session.mjs call POST /api/research/debates --data '{"symbol":"SHFE.rb2701"}'
//   node scripts/test_session.mjs call POST /api/... --data @body.json --account <账户id>
//
// Every `call` first checks that the saved session is the test account (its username must equal
// the account name of the Keychain item `ai-trader-tqkq-test-account`, or
// AI_TRADER_TEST_SESSION_USER); anything else — not signed in, the owner's account — is refused,
// so a mis-signed profile can never write to the owner's account.
// UI scripts: `chromium.launchPersistentContext(TEST_SESSION_PROFILE, …)` with the exported path,
// after `status` has confirmed the identity.
// One process at a time: Chromium locks the profile while it is open.
import { chromium } from "playwright";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

export const TEST_SESSION_PROFILE = process.env.AI_TRADER_TEST_SESSION_PROFILE
  || join(homedir(), ".cache", "ai-trader", "test-session-profile");
const SITE = process.env.AI_TRADER_TEST_SESSION_SITE || "https://qilinshuzhi.com/trader/";
const RUNTIME = process.env.AI_TRADER_TEST_SESSION_RUNTIME || "https://qilinshuzhi.com/trader-runtime";
const STATUS_PATH = "/api/auth/owner/status";

/** The test account's username: the Keychain item's account attribute (never its password). */
export function expectedTestUser() {
  if (process.env.AI_TRADER_TEST_SESSION_USER) return process.env.AI_TRADER_TEST_SESSION_USER;
  try {
    const out = execFileSync("security", ["find-generic-password", "-s", "ai-trader-tqkq-test-account"],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    return /"acct"<blob>="([^"]*)"/.exec(out)?.[1] || null;
  } catch {
    return null;
  }
}

async function openContext(headless) {
  mkdirSync(TEST_SESSION_PROFILE, { recursive: true });
  // The owner's login window follows its real window size (a fixed viewport clipped the app's right
  // rail and bottom panel in a smaller window); headless runs keep the 1440×960 acceptance size.
  return chromium.launchPersistentContext(TEST_SESSION_PROFILE, {
    headless, viewport: headless ? { width: 1440, height: 960 } : null, colorScheme: "dark",
  });
}

// Runs inside the site's own page, so the request carries the session cookie exactly as the web
// client does (and, with --account, the same X-AI-Trader-Account header).
function fetchInPage(page, method, path, body, account) {
  return page.evaluate(async ({ url, method, body, account }) => {
    const headers = { "X-AI-Trader-Client": "test-session" };
    if (account) headers["X-AI-Trader-Account"] = account;
    const init = { method, credentials: "include", headers };
    if (body !== undefined) { headers["Content-Type"] = "application/json"; init.body = body; }
    const response = await fetch(url, init);
    const text = await response.text();
    let parsed = text;
    try { parsed = JSON.parse(text); } catch { /* not JSON */ }
    return { status: response.status, body: parsed };
  }, { url: `${RUNTIME}${path}`, method, body, account });
}

/** Whether a status reply is the test account; the reason when not. */
export function identityProblem(status, expected) {
  if (!expected) return "找不到测试账号的用户名（Keychain ai-trader-tqkq-test-account 或 AI_TRADER_TEST_SESSION_USER）";
  if (status?.status !== 200 || !status.body?.authenticated) return "保存的会话没有登录或已过期，请主人重新运行 login";
  if (status.body.username !== expected) return "保存的会话不是快期测试账号，拒绝调用；请主人用测试账号重新运行 login";
  return null;
}

async function session(work) {
  const context = await openContext(true);
  try {
    const page = context.pages()[0] ?? await context.newPage();
    await page.goto(SITE, { waitUntil: "domcontentloaded" });
    return await work(page);
  } finally {
    await context.close();
  }
}

async function login() {
  const context = await openContext(false);
  const page = context.pages()[0] ?? await context.newPage();
  await page.goto(SITE);
  console.log("在打开的窗口里用快期测试账号登录 Sente（密码由你输入），看到工作台后关闭窗口即可。");
  // On macOS closing the window may close only the page, not the browser: either ends the wait.
  await new Promise(resolve => { page.once("close", resolve); context.once("close", resolve); });
  await context.close().catch(() => {});
  await status();
}

async function status() {
  const reply = await session(page => fetchInPage(page, "GET", STATUS_PATH));
  const problem = identityProblem(reply, expectedTestUser());
  console.log(JSON.stringify({ status: reply.status, authenticated: Boolean(reply.body?.authenticated),
    isTestAccount: problem === null, plan: reply.body?.plan ?? null, problem }, null, 2));
  process.exitCode = problem ? 1 : 0;
}

async function call(method, path, body, account) {
  const result = await session(async page => {
    const problem = identityProblem(await fetchInPage(page, "GET", STATUS_PATH), expectedTestUser());
    if (problem) return { status: 0, body: { refused: problem } };
    return fetchInPage(page, method, path, body, account);
  });
  console.log(JSON.stringify(result, null, 2));
  process.exitCode = result.status >= 200 && result.status < 400 ? 0 : 1;
}

function option(args, name) {
  const at = args.indexOf(name);
  return at >= 0 ? args[at + 1] : undefined;
}

async function main() {
  const [command, method, path, ...rest] = process.argv.slice(2);
  if (command === "login") return login();
  if (command === "status") return status();
  if (command === "call" && method && path) {
    const data = option(rest, "--data");
    const body = data === undefined ? undefined : (data.startsWith("@") ? readFileSync(data.slice(1), "utf8") : data);
    return call(method.toUpperCase(), path, body, option(rest, "--account"));
  }
  console.error("用法：test_session.mjs login | status | call <METHOD> <PATH> [--data JSON|@file] [--account <账户id>]");
  process.exitCode = 2;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
