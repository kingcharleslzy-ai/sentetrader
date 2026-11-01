#!/usr/bin/env node
// 上线回读 (owner 2026-10-25): one fixed command after every web + Runtime release, instead of a script written
// afresh each time. It reuses the TqKq test account's saved session (scripts/test_session.mjs — agents never type
// its password) and checks, every time:
//   - the saved session is the test account; the web build and the Runtime commit are the ones just released;
//     liveTrading is disabled;
//   - one web window registers; every UI command of the plan is applied and the snapshot shows what the plan
//     expects (polled until it does or the step times out, so a page still loading is not a false failure);
//   - no page error, console error or 5xx in that window; UI-triggered writes to orders, alerts, debates and
//     strategies are blocked unless the plan allows them;
//   - for --monitor seconds (default 600, the release rule's 10 minutes) both ends keep the released commits and
//     liveTrading disabled, the hosts' nginx logs show no new 5xx and the production containers no ERROR /
//     Traceback.
// What a batch adds is a plan file (scripts/readback/*.json): its UI commands, API steps and cleanup. A plan says
// what it checks; anything it leaves out — layout, colours, market-hours behaviour, the phone, voice — still needs
// the screenshots it takes to be looked at.
//
//   node scripts/readback.mjs --plan scripts/readback/<plan>.json --runtime <sha> --web <sha> [--out <dir>]
//        [--monitor <seconds>] [--no-monitor]
//   node scripts/readback.mjs --selftest --runtime <sha> --web <sha>   # a wrong expectation must fail the run
//
// Plan steps (run in order; `cleanup` steps always run afterwards, then the window's view and panel are restored):
//   {"ui": {<UI command changes>}, "expect": {"panel": "news", "news.tab": "debate"}}
//   {"api": "PUT /api/account/trade-playbooks/{{roundId}}", "body": {...}, "status": 200,
//    "expect": {"body.playbookName": "验收打法"}, "save": {"roundId": "body.trades.0.id"}}
//   {"screenshot": "01-review", "scrollTo": "这笔的打法"}      (scrollTo: a control's accessible name)
//   {"fill": {"label": "止损价", "value": "3000"}}              (types into a field by its accessible name; never submits)
// `{{name}}` anywhere in a step is replaced by a value an earlier step saved; expect paths are dotted (array
// indexes allowed); an expected value of {"exists": true} only asks that the path be there, {"includes": "x"}
// that its text (an object's JSON, e.g. `observation.controls` with selects' options) contain x. A plan's
// "device": "phone" runs the window in the phone layout. A UI step's paths read the window state, plus `observation.text` (the snapshot's
// on-screen text).
import { chromium } from "playwright";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { TEST_SESSION_PROFILE, expectedTestUser, identityProblem } from "./test_session.mjs";

const SITE = process.env.AI_TRADER_TEST_SESSION_SITE || "https://qilinshuzhi.com/trader/";
const ORIGIN = new URL(SITE).origin;
const HOSTS = [["runtime", "tencent-ai-trader", "sudo "], ["web", "aliyun-qilinshuzhi", ""]];
const BLOCKED_WRITES = /\/api\/(simulation|proposals|strategies|strategy-backtests|market\/alerts|research\/debates|research\/desk\/refresh)(\/|$)/;

const option = (name, fallback) => { const at = process.argv.indexOf(name); return at >= 0 ? process.argv[at + 1] : fallback; };
const flag = name => process.argv.includes(name);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/** The value at a dotted path ("news.tab", "body.trades.0.id"); undefined when any part is missing. */
export function valueAt(source, path) {
  return path.split(".").reduce((value, key) => (value == null ? undefined : value[key]), source);
}

/** The expectations a value does not meet: [] when it meets them all. */
export function unmet(source, expect = {}) {
  return Object.entries(expect).flatMap(([path, wanted]) => {
    const actual = valueAt(source, path);
    const ok = wanted && typeof wanted === "object" && "exists" in wanted ? (actual !== undefined) === wanted.exists
      : wanted && typeof wanted === "object" && "includes" in wanted
        ? (actual && typeof actual === "object" ? JSON.stringify(actual) : String(actual ?? "")).includes(wanted.includes)
      : JSON.stringify(actual) === JSON.stringify(wanted);
    return ok ? [] : [`${path}: 期望 ${JSON.stringify(wanted)}，实际 ${JSON.stringify(actual)}`];
  });
}

/** `{{name}}` in every string of a step replaced by a saved value. */
export function fill(step, saved) {
  return JSON.parse(JSON.stringify(step), (_, value) => typeof value === "string"
    ? value.replace(/\{\{(\w+)\}\}/g, (match, name) => (name in saved ? String(saved[name]) : match)) : value);
}

async function until(read, label, ms = 30_000) {
  const end = Date.now() + ms;
  let last;
  while (Date.now() < end) {
    last = await read();
    if (last?.ok) return last.value;
    await sleep(250);
  }
  throw new Error(`${label}${last?.why ? `：${last.why}` : "（超时）"}`);
}

function ssh(host, command) {
  return execFileSync("ssh", ["-n", "-o", "ConnectTimeout=15", host, command], { encoding: "utf8" });
}

/** Where each host's nginx access log stands now, so the monitor reads only what comes after. */
function logMarks() {
  return Object.fromEntries(HOSTS.map(([name, host, sudo]) => [name, JSON.parse(ssh(host,
    `${sudo}python3 -c 'import os,json;s=os.stat("/var/log/nginx/access.log");print(json.dumps(dict(inode=s.st_ino,size=s.st_size)))'`))]));
}

function logWindow(marks, startedIso) {
  const out = {};
  for (const [name, host, sudo] of HOSTS) {
    const { inode, size } = marks[name];
    // The nightly rotation moves the marked file to access.log.1: then read its tail and the whole new file.
    const raw = ssh(host, `${sudo}python3 -c 'import os,sys;p="/var/log/nginx/access.log";moved=os.stat(p).st_ino!=${inode};f=open(p+".1" if moved else p,"rb");assert os.fstat(f.fileno()).st_ino==${inode};f.seek(${size});sys.stdout.buffer.write(f.read()+(open(p,"rb").read() if moved else b""))'`);
    const rows = [...raw.matchAll(/"(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) ([^ ]+) HTTP\/[^"]+" (\d{3}) /g)]
      .map(match => ({ path: match[1], status: Number(match[2]) }))
      .filter(row => name === "runtime" || row.path.startsWith("/trader") || row.path.startsWith("/ai-trader"));
    out[name] = { requests: rows.length, errors5xx: rows.filter(row => row.status >= 500) };
  }
  const containers = ssh("tencent-ai-trader", "sudo docker ps --format '{{.Names}}'").split("\n").filter(name => name.startsWith("ai-trader-production-"));
  out.containerErrors = Object.fromEntries(containers.map(name => [name,
    ssh("tencent-ai-trader", `sudo docker logs --since ${startedIso} ${name} 2>&1`).split("\n").filter(line => /\bERROR\b|Traceback/.test(line))])
    .filter(([, lines]) => lines.length));
  return out;
}

async function readVersions(page) {
  return page.evaluate(async origin => {
    const read = async url => { const response = await fetch(url, { cache: "no-store", credentials: "include" }); return response.json(); };
    return { web: await read(`${origin}/trader/version.json`), runtime: await read(`${origin}/trader-runtime/healthz`) };
  }, ORIGIN);
}

function versionProblems(versions, expected) {
  return [
    ...(String(versions.web?.commit ?? "").startsWith(expected.web) ? [] : [`网页版本 ${versions.web?.commit} 不是 ${expected.web}`]),
    ...(String(versions.runtime?.commit ?? "").startsWith(expected.runtime) ? [] : [`Runtime ${versions.runtime?.commit} 不是 ${expected.runtime}`]),
    ...(versions.runtime?.liveTrading === "disabled" ? [] : [`liveTrading 是 ${versions.runtime?.liveTrading}`]),
  ];
}

async function run({ plan, expected, out, monitorSeconds }) {
  mkdirSync(out, { recursive: true });
  const started = new Date();
  const result = { started: started.toISOString(), expected, steps: [], cleanup: [], pageErrors: [], consoleErrors: [], errors5xx: [], blockedWrites: [] };
  const marks = monitorSeconds ? logMarks() : null;
  // A plan with "device": "phone" reads the phone layout (narrow and touch: the client's (max-width: 760px) and (pointer: coarse)).
  const context = await chromium.launchPersistentContext(TEST_SESSION_PROFILE, plan.device === "phone"
    ? { headless: true, viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true }
    : { headless: true, viewport: { width: 1440, height: 900 } });
  const page = context.pages()[0] ?? await context.newPage();
  const allowed = new RegExp(plan.allowWrites?.length ? plan.allowWrites.join("|") : "$^");
  // Once the window registers, every API step speaks for the window's trading account: the session's default
  // account can be another one, whose round trip ids the window would refuse (10-25 第 2 批后半回读).
  let windowId, account; const saved = {}; let original;
  page.on("pageerror", error => result.pageErrors.push(String(error)));
  page.on("console", message => { if (message.type() === "error") result.consoleErrors.push(message.text()); });
  result.failedRequests = [];
  page.on("response", response => {
    if (response.status() >= 500) result.errors5xx.push({ url: response.url(), status: response.status() });
    // A console "Failed to load resource" names no address; this list does.
    if (response.status() >= 400) result.failedRequests.push(`${response.status()} ${response.request().method()} ${new URL(response.url()).pathname}`);
    if (new URL(response.url()).pathname.endsWith("/api/ui/pulse") && response.ok()) {
      const body = response.request().postDataJSON();
      if (String(body?.device?.build ?? "").startsWith(expected.web)) windowId = body.windowId;
    }
  });
  await page.route("**/trader-runtime/api/**", route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() !== "GET" && BLOCKED_WRITES.test(path) && !allowed.test(path)) {
      result.blockedWrites.push(`${request.method()} ${path}`);
      return route.abort();
    }
    return route.continue();
  });
  const api = (method, path, body) => page.evaluate(async ({ method, path, body, account }) => {
    const headers = { "Content-Type": "application/json", "X-AI-Trader-Client": "readback" };
    if (account) headers["X-AI-Trader-Account"] = account;
    const response = await fetch(`/trader-runtime${path}`, { method, headers, credentials: "include", body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await response.text();
    let parsed = text; try { parsed = JSON.parse(text); } catch { /* not JSON */ }
    return { status: response.status, attempted: response.headers.get("X-UI-Command-Attempted"), body: parsed };
  }, { method, path, body, account });
  const detail = async () => (windowId ? (await api("GET", `/api/app/ui/windows/${windowId}`)).body : null);
  const command = async changes => {
    let previous, stable = 0;
    let snapshot = await until(async () => {
      const current = await detail();
      stable = current?.revision !== undefined && current.revision === previous ? stable + 1 : 0;
      previous = current?.revision;
      return { ok: stable >= 3, value: current };
    }, "窗口状态稳定");
    let reply;
    for (let attempt = 0; attempt < 3; attempt++) {
      const commandId = randomUUID();
      reply = await api("POST", "/api/app/ui/commands?waitMs=3000", { commandId, windowId, expectedRevision: snapshot.revision, changes });
      if (reply.status === 409 && reply.attempted === "false") { snapshot = await detail(); continue; }
      if (reply.status >= 400) throw new Error(`界面命令 ${JSON.stringify(changes)} 被拒（${reply.status}）：${JSON.stringify(reply.body).slice(0, 300)}`);
      if (["queued", "dispatched"].includes(reply.body?.status)) {
        reply = await until(async () => { const r = await api("GET", `/api/app/ui/commands/${commandId}`); return { ok: !["queued", "dispatched"].includes(r.body?.status), value: r }; }, "界面命令回执");
      }
      // Rejected with no state: the window had moved on before it ran the command (the same signal the agent re-sends on).
      if (reply.body?.status !== "rejected" || reply.body?.state != null) break;
      snapshot = await detail();
    }
    if (reply.status >= 400) throw new Error(`界面命令 ${JSON.stringify(changes)} 被拒（${reply.status}）：${JSON.stringify(reply.body).slice(0, 300)}`);
    return reply.body;
  };
  const step = async (raw, log) => {
    const item = fill(raw, saved);
    const entry = { step: item };
    log.push(entry);
    if (item.ui) {
      const receipt = await command(item.ui);
      entry.receipt = receipt?.status;
      // A refused command keeps the whole receipt (the window's state and revision when it answered).
      if (receipt?.status !== "applied") entry.reply = receipt;
      if (receipt?.status !== "applied") throw new Error(`界面命令 ${JSON.stringify(item.ui)} 回执 ${receipt?.status}：${receipt?.reason ?? ""}`);
      // Paths read the window's state; `observation.text` is what the snapshot sees on screen.
      entry.state = await until(async () => { const current = await detail(); const why = unmet({ ...current?.state, observation: current?.observation }, item.expect); return { ok: !why.length, value: current?.state, why: why.join("；") }; },
        `界面命令 ${JSON.stringify(item.ui)} 之后的快照`, item.timeoutMs ?? 15_000);
    } else if (item.api) {
      const [method, path] = item.api.split(" ");
      const reply = await api(method, path, item.body);
      entry.status = reply.status;
      const why = [...(reply.status === (item.status ?? 200) ? [] : [`状态 ${reply.status}，期望 ${item.status ?? 200}：${JSON.stringify(reply.body).slice(0, 300)}`]), ...unmet(reply, item.expect)];
      if (why.length) throw new Error(`${item.api}：${why.join("；")}`);
      for (const [name, path] of Object.entries(item.save ?? {})) saved[name] = valueAt(reply, path);
      writeFileSync(join(out, `api-${log.length}.json`), JSON.stringify(reply, null, 2));
    } else if (item.fill) {
      // Types into a field the way the user does (no order is placed by typing): controls that appear only
      // once a value is filled in can then be seen and screenshot. Only the window's own inputs, never a submit.
      await page.getByLabel(item.fill.label, { exact: true }).first().fill(String(item.fill.value));
    } else if (item.screenshot) {
      // A control inside a panel's own scroll area is off a full-page shot: bring it into view first.
      if (item.scrollTo) await page.getByLabel(item.scrollTo, { exact: true }).first().scrollIntoViewIfNeeded();
      await page.screenshot({ path: join(out, `${item.screenshot}.png`), fullPage: true });
    }
  };
  try {
    await page.goto(SITE, { waitUntil: "domcontentloaded" });
    const problem = identityProblem(await api("GET", "/api/auth/owner/status"), expectedTestUser());
    if (problem) throw new Error(problem);
    const versionProblem = versionProblems(result.versions = await readVersions(page), expected);
    if (versionProblem.length) throw new Error(versionProblem.join("；"));
    original = await until(async () => { const current = await detail(); return { ok: Boolean(current?.state?.view && current.state.tradingAccount), value: current?.state }; }, "网页窗口登记", 60_000);
    account = original.tradingAccount;
    result.account = account;
    for (const raw of plan.steps ?? []) await step(raw, result.steps);
  } catch (error) {
    result.failure = String(error.message ?? error);
    await page.screenshot({ path: join(out, "failure.png"), fullPage: true }).catch(() => {});
  } finally {
    for (const raw of plan.cleanup ?? []) {
      try { await step(raw, result.cleanup); } catch (error) { result.cleanupFailure = [...(result.cleanupFailure ?? []), String(error.message ?? error)]; }
    }
    // One interface field per command (the Runtime refuses more): the view, then the panel.
    if (original) {
      await command({ view: original.view }).then(() => command({ panel: original.panel ?? null }))
        .catch(error => { result.restoreFailure = String(error.message ?? error); });
    }
    await context.close();
  }
  if (monitorSeconds) {
    const samples = [];
    const contextless = await chromium.launch();
    const probe = await contextless.newPage();
    await probe.goto(SITE, { waitUntil: "domcontentloaded" });
    while ((Date.now() - started.getTime()) / 1000 < monitorSeconds) {
      const versions = await readVersions(probe).catch(error => ({ error: String(error) }));
      samples.push({ at: new Date().toISOString(), problems: versions.error ? [versions.error] : versionProblems(versions, expected) });
      await sleep(30_000);
    }
    await contextless.close();
    result.monitor = { seconds: Math.round((Date.now() - started.getTime()) / 1000), samples: samples.length,
      versionProblems: samples.flatMap(sample => sample.problems), ...logWindow(marks, result.started) };
  }
  const problems = [
    ...(result.failure ? [result.failure] : []), ...(result.cleanupFailure ?? []), ...(result.restoreFailure ? [`恢复窗口：${result.restoreFailure}`] : []),
    ...result.pageErrors.map(e => `页面报错：${e}`), ...result.consoleErrors.map(e => `控制台报错：${e}`),
    ...result.errors5xx.map(e => `浏览器 5xx：${e.status} ${e.url}`), ...result.blockedWrites.map(w => `拦下的写入：${w}`),
    ...(result.monitor ? [...result.monitor.versionProblems, ...result.monitor.runtime.errors5xx.map(e => `Runtime 5xx：${e.status} ${e.path}`),
      ...result.monitor.web.errors5xx.map(e => `网页 5xx：${e.status} ${e.path}`),
      ...Object.entries(result.monitor.containerErrors).map(([name, lines]) => `${name} 有 ${lines.length} 行 ERROR/Traceback`)] : []),
  ];
  result.passed = problems.length === 0;
  result.problems = problems;
  writeFileSync(join(out, "result.json"), JSON.stringify(result, null, 2));
  return result;
}

async function main() {
  const expected = { runtime: option("--runtime"), web: option("--web") };
  if (!expected.runtime || !expected.web) {
    console.error("用法：readback.mjs --plan <plan.json> --runtime <sha> --web <sha> [--out <dir>] [--monitor <秒>] [--no-monitor] | --selftest --runtime <sha> --web <sha>");
    process.exitCode = 2;
    return;
  }
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const out = option("--out", join(homedir(), "ai-trader-release-artifacts", `readback-${stamp}`));
  if (flag("--selftest")) {
    // The checker must be able to fail: a snapshot field no window ever reports has to stop the run.
    const result = await run({ plan: { steps: [{ ui: { view: "trade" }, expect: { panel: "never-a-panel" }, timeoutMs: 3000 }] }, expected, out, monitorSeconds: 0 });
    const caught = !result.passed && /never-a-panel/.test(result.failure ?? "");
    console.log(JSON.stringify({ selftest: caught ? "ok：错误的期望被报出" : "失败：错误的期望没有被报出", problems: result.problems, out }, null, 2));
    process.exitCode = caught ? 0 : 1;
    return;
  }
  const planPath = option("--plan");
  const plan = planPath ? JSON.parse(readFileSync(planPath, "utf8")) : { steps: [] };
  const monitorSeconds = flag("--no-monitor") ? 0 : Number(option("--monitor", "600"));
  const result = await run({ plan, expected, out, monitorSeconds });
  console.log(JSON.stringify({ passed: result.passed, problems: result.problems, steps: result.steps.length, cleanup: result.cleanup.length,
    monitor: result.monitor && { seconds: result.monitor.seconds, runtimeRequests: result.monitor.runtime.requests, webRequests: result.monitor.web.requests }, out }, null, 2));
  process.exitCode = result.passed ? 0 : 1;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
