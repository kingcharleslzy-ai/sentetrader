// @vitest-environment jsdom
// Stays on jsdom: its assertions fail under the default happy-dom (accessible names / text / location differ).
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import { AdminApp } from "./AdminApp";
import { isAdminPath } from "./adminRoute";

// 用户体验's time series draws on lightweight-charts; jsdom has no canvas.
const chartData = vi.hoisted(() => [] as unknown[]);
vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  LineSeries: {},
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  createChart: () => ({
    addSeries: () => ({ setData: (data: unknown) => chartData.push(data), applyOptions: () => undefined }),
    applyOptions: () => undefined,
    timeScale: () => ({ fitContent: () => undefined }),
    remove: () => undefined,
  }),
}));

type InviteRow = {
  id: string; code: string | null; uses: "once" | "unlimited"; useCount: number;
  usedBy: Array<{ username: string; usedAt: string | null }>; plan: string | null;
  createdBy: string; createdAt: string; expiresAt: string | null; disabledAt: string | null;
  status: "active" | "used_up" | "expired" | "disabled";
};

/** The /trader/admin page's Runtime: the owner-status read plus the admin surfaces the
 *  console reads per section. `role` picks the gate ("owner" opens the console);
 *  the guest tier joins the plan list with `guest`. Invite rows carry the four
 *  capabilities (custom code, uses, never-expires, disable). */
function adminRuntime(options: { role: "owner" | "member" | "signed-out"; registry?: boolean; guest?: boolean }) {
  const ownerStatus = options.role === "owner"
    ? { authenticated: true, authMode: "owner-password", authProvider: "owner-password", username: "boss", userId: "owner-boss", role: "owner", plan: "owner", planLabel: "管理员",
        features: { aiExecution: true, webResearch: true, strategyTools: true, marketBriefs: true, strategies: true, backtests: true, replays: true, voice: true } }
    : options.role === "member"
      ? { authenticated: true, authMode: "owner-password", authProvider: "owner-password", username: "u1", userId: "owner-u1", role: "user", plan: "basic", planLabel: "基础档",
          features: { aiExecution: false, webResearch: false, strategyTools: false, marketBriefs: true, strategies: true, backtests: false, replays: false, voice: true } }
      : { authenticated: false, authMode: null, authProvider: null, username: null, userId: null, role: null, plan: null, planLabel: null, features: {} };
  let signedIn = options.role !== "signed-out";
  let users = [{ username: "u1", role: "user", plan: "basic", planLabel: "基础档", invitedBy: "invite-code", createdAt: "2026-09-20T08:00:00Z", lastSeenAt: "2026-09-22T02:04:00Z",
    tokensToday: 1_000, tokensMonth: 5_000, voiceSecondsToday: 60, voiceSecondsMonth: 120, estimatedCostToday: 1.23, disabled: false, tqkqBound: true }];
  let invites: InviteRow[] = [
    { id: "old-1", code: null, uses: "once", useCount: 1, usedBy: [{ username: "u1", usedAt: "2026-09-21T02:00:00Z" }], plan: null,
      createdBy: "boss", createdAt: "2026-09-20T01:00:00Z", expiresAt: "2026-10-20T00:00:00Z", disabledAt: null, status: "used_up" },   // 旧码：只存了摘要
    { id: "inv-2", code: "GATE-2026", uses: "once", useCount: 0, usedBy: [], plan: "basic",
      createdBy: "boss", createdAt: "2026-09-22T01:00:00Z", expiresAt: "2026-10-06T00:00:00Z", disabledAt: null, status: "active" },
    { id: "inv-3", code: "open-door", uses: "unlimited", useCount: 2,
      usedBy: [{ username: "u2", usedAt: "2026-09-22T05:00:00Z" }, { username: "u3", usedAt: "2026-09-22T06:00:00Z" }], plan: null,
      createdBy: "boss", createdAt: "2026-09-21T01:00:00Z", expiresAt: "2026-10-05T00:00:00Z", disabledAt: null, status: "active" },
    { id: "inv-4", code: "OLD-EXPIRED", uses: "once", useCount: 0, usedBy: [], plan: null,
      createdBy: "boss", createdAt: "2026-09-01T01:00:00Z", expiresAt: "2026-09-25T00:00:00Z", disabledAt: null, status: "expired" },
  ];
  const modelCatalog = [{ slug: "deepseek-flash", label: "DeepSeek Flash" }, { slug: "glm-5.3-flash", label: "GLM 5.3 Flash" }];
  let adminSkills: Array<{ id: string; label: string; category: string; description: string; parent: string; summary: string; tierGated: boolean; ownerOnly: boolean; enabled: boolean; tiers: Record<string, boolean> }> = [
    // Id order, as the Runtime lists them: one category's rows are not adjacent.
    { id: "admin-console", label: "后台管理", category: "系统", description: "成员与档位管理控制台", parent: "", summary: "后台管理（仅主账号）", tierGated: true, ownerOnly: true, enabled: true, tiers: {} },
    { id: "ai-execution", label: "AI 自主执行", category: "交易", description: "AI 直接执行模拟下单与止损", parent: "", summary: "让 AI 在你设定的品种、手数、笔数和时长内自己下模拟单", tierGated: true, ownerOnly: false, enabled: true, tiers: { basic: false, pro: true } },
    { id: "help", label: "使用说明", category: "系统", description: "内置手册与操作指引", parent: "", summary: "使用说明和常见问题", tierGated: false, ownerOnly: false, enabled: true, tiers: {} },
    { id: "strategy-management", label: "策略管理与监控", category: "策略", description: "策略的创建、保存、上线与监控", parent: "", summary: "创建、保存、上线和监控策略", tierGated: true, ownerOnly: false, enabled: true, tiers: { basic: true, pro: true } },
    { id: "strategy-signals", label: "策略信号解读", category: "策略", description: "解读当前策略的信号并标注", parent: "strategy-management", summary: "解读运行中策略发出的开平仓信号", tierGated: true, ownerOnly: false, enabled: true, tiers: { basic: true, pro: true } }];
  const openTierSkills = ["ai-execution", "web-research", "market-briefs", "strategies", "strategy-signals",
    "strategy-backtests", "market-replay", "replay-control", "voice-call"];
  let tiers: Record<string, unknown>[] = [
    { name: "basic", label: "基础档", isDefault: true, models: ["deepseek-flash"], dailyTokens: 300_000, voiceSecondsPerDay: 1_800, briefProducts: 3,
      skills: openTierSkills.filter(id => id !== "ai-execution" && id !== "web-research"), userCount: 1 },
    { name: "pro", label: "高级档", isDefault: false, models: null, dailyTokens: 2_000_000, voiceSecondsPerDay: 7_200,
      skills: openTierSkills, userCount: 0 }];
  if (options.guest) tiers.push({ name: "guest", label: "游客", isDefault: false, models: [], dailyTokens: 0,
    voiceSecondsPerDay: 60, marketHistoryTradingDays: 3, skills: ["voice-call"], userCount: 0 });
  let failSkillWrites = false;
  let inviteFailure: number | null = null;
  const calls: { method: string; path: string; body?: Record<string, unknown> }[] = [];
  const json = (data: unknown) => new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
  const fetch = vi.fn(async (input: Request | string, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    const body = request.body == null ? undefined : JSON.parse(await request.text());
    calls.push({ method: request.method, path, body });
    if (failSkillWrites && request.method === "PUT" && (path.startsWith("/api/admin/skills/") || path.startsWith("/api/admin/plans/"))) {
      return new Response("boom", { status: 500 });
    }
    if (path === "/api/auth/owner/status") return json(signedIn ? ownerStatus : { ...ownerStatus, authenticated: false });
    if (path === "/api/auth/owner/logout") { signedIn = false; return json({ status: "signed_out" }); }
    if (!options.registry && path === "/api/agent/skills") return new Response("not found", { status: 404 });
    if (path === "/api/admin/skills") return json({ items: adminSkills });
    if (path.startsWith("/api/admin/skills/") && request.method === "PUT") {
      const skillId = path.split("/").pop() ?? "";
      adminSkills = adminSkills.map(skill => skill.id === skillId ? { ...skill, enabled: Boolean(body?.enabled) } : skill);
      return json({ items: adminSkills });
    }
    if (path === "/api/admin/users" && request.method === "GET") return json({ items: users, day: "2026-09-22" });
    if (path === "/api/admin/users/u1") {
      users = [{ ...users[0], ...(body as Record<string, unknown>), planLabel: (body as { plan?: string }).plan === "pro" ? "高级档" : "基础档" }];
      return json(users[0]);
    }
    if (path === "/api/admin/plans" && request.method === "GET") return json({ items: tiers, modelCatalog });
    if (path === "/api/admin/plans" && request.method === "POST") {
      const created = { name: "tier-abc123", label: (body as { label: string }).label, isDefault: false,
        models: ["deepseek-flash"], dailyTokens: 300_000, voiceSecondsPerDay: 1_800,
        skills: openTierSkills.filter(id => id !== "ai-execution" && id !== "web-research"), userCount: 0 };
      tiers = [...tiers, created];
      return json(created);
    }
    if (path.startsWith("/api/admin/plans/") && request.method === "PUT") {
      const name = path.split("/").pop() ?? "";
      if (body?.skills) {  // the tier checkbox merge: flip the mentioned ids, keep the rest
        const flips = body.skills as Record<string, boolean>;
        adminSkills = adminSkills.map(skill => flips[skill.id] === undefined ? skill
          : { ...skill, tiers: { ...skill.tiers, [name]: flips[skill.id] } });
      }
      tiers = tiers.map(tier => {
        if (tier.name !== name) return body?.isDefault ? { ...tier, isDefault: false } : tier;
        const merged = { ...tier, ...(body as Record<string, unknown>) };
        if (body?.skills) {
          // body.skills is the flips map, not the list — start from the tier's own list.
          const ids = new Set(tier.skills as string[]);
          for (const [id, open] of Object.entries(body.skills as Record<string, boolean>)) {
            if (open) ids.add(id); else ids.delete(id);
          }
          merged.skills = [...ids];
        }
        return merged;
      });
      return json(tiers.find(tier => tier.name === name));
    }
    if (path.startsWith("/api/admin/plans/") && request.method === "DELETE") {
      const name = path.split("/").pop();
      tiers = tiers.filter(tier => tier.name !== name);
      return json({ status: "deleted", name });
    }
    if (path === "/api/auth/owner/invite-codes" && request.method === "POST") {
      if (inviteFailure != null) {
        const status = inviteFailure;
        inviteFailure = null;
        return new Response(JSON.stringify({ detail: status === 422 ? "invite_code_format" : "invite_code_exists" }),
          { status, headers: { "Content-Type": "application/json" } });
      }
      const request2 = body as { code?: string; uses?: "once" | "unlimited"; plan?: string; neverExpires?: boolean };
      const code = request2.code ?? "invite-abc123";
      const row: InviteRow = { id: `inv-new-${invites.length}`, code, uses: request2.uses ?? "once", useCount: 0, usedBy: [],
        plan: request2.plan ?? null, createdBy: "boss", createdAt: "2026-09-22T03:00:00Z",
        expiresAt: request2.neverExpires ? null : "2026-10-06T00:00:00Z", disabledAt: null, status: "active" };
      invites = [row, ...invites];
      return json({ inviteCode: code, expiresAt: row.expiresAt, plan: request2.plan ?? "", uses: row.uses });
    }
    if (/^\/api\/auth\/owner\/invite-codes\/[^/]+\/disable$/.test(path) && request.method === "POST") {
      const id = path.split("/")[5];
      const row = invites.find(item => item.id === id);
      if (!row) return new Response(JSON.stringify({ detail: "invite_code_unknown" }), { status: 404 });
      invites = invites.map(item => item.id === id
        ? { ...item, status: "disabled" as const, disabledAt: "2026-09-22T07:00:00Z" } : item);
      return json(invites.find(item => item.id === id));
    }
    if (path === "/api/auth/owner/invite-codes") return json(invites);
    if (path === "/api/admin/accounts") return json({ items: [
      { id: "acc-1", displayName: "TqKq 模拟账户", providerKind: "tqkq-sidecar", status: "active", executionOwner: "boss",
        grants: [{ user: "boss", role: "owner" }, { user: "u1", role: "viewer" }] }] });
    return json({ items: [] });
  });
  vi.stubGlobal("fetch", fetch);
  return { calls, users: () => users, tiers: () => tiers, invites: () => invites,
    failSkillWrites: (value: boolean) => { failSkillWrites = value; },
    failNextInviteWith: (status: number) => { inviteFailure = status; } };
}

test("isAdminPath：只认基路径下的 admin（含尾斜杠），其余路径走交易页", () => {
  expect(isAdminPath("/trader/admin", "/trader/")).toBe(true);
  expect(isAdminPath("/trader/admin/", "/trader/")).toBe(true);
  expect(isAdminPath("/trader/", "/trader/")).toBe(false);
  expect(isAdminPath("/trader/adminx", "/trader/")).toBe(false);
  expect(isAdminPath("/app/admin", "/trader/")).toBe(false);
  expect(isAdminPath("/admin", "/")).toBe(true);
});

test("未登录：整页登录框（无取消），不渲染控制台", async () => {
  adminRuntime({ role: "signed-out" });
  render(<AdminApp />);
  const sheet = await screen.findByRole("dialog", { name: "登录 Sente" });
  expect(within(sheet).getByLabelText("账号")).toBeInTheDocument();
  // required 登录门：没有取消按钮，也没有控制台。
  expect(within(sheet).queryByRole("button", { name: "取消" })).toBeNull();
  expect(screen.queryByRole("region", { name: "后台管理" })).toBeNull();
});

test("非管理员：居中提示无权限，退出登录走登出接口并回到登录框", async () => {
  const runtime = adminRuntime({ role: "member" });
  render(<AdminApp />);
  const gate = await screen.findByRole("heading", { name: "后台只对管理员开放" });
  expect(gate).toBeInTheDocument();
  expect(screen.getByText("当前账号：u1")).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "后台管理" })).toBeNull();
  expect(screen.getByRole("link", { name: "打开交易页" })).toHaveAttribute("href", "/trader/");
  fireEvent.click(screen.getByRole("button", { name: "退出登录" }));
  await waitFor(() => expect(runtime.calls.find(call => call.path === "/api/auth/owner/logout" && call.method === "POST")).toBeTruthy());
  expect(await screen.findByRole("dialog", { name: "登录 Sente" })).toBeInTheDocument();
});

test("管理员：顶栏带账号与交易页链接；概览 tiles、档位/邀请码跳转、导航计数", async () => {
  adminRuntime({ role: "owner" });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  expect(screen.getByText("boss")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "打开交易页" })).toHaveAttribute("href", "/trader/");
  // 独立页没有工作台可回：控制台不渲染返回按钮。
  expect(within(console).queryByRole("button", { name: "返回工作台" })).toBeNull();
  // 今日 tiles：成员不含主账号，token/成本来自成员行。
  await waitFor(() => expect(within(console).getByText("今日 token")).toBeInTheDocument());
  expect(within(console).getByText("1,000")).toBeInTheDocument();
  expect(within(console).getByText("1.23 元")).toBeInTheDocument();
  expect(within(console).getByText("成员")).toBeInTheDocument();
  // 用量按 UTC 日结算：北京时间早上 8 点清零，不是零点。这是读数口径，常驻显示。
  expect(within(console).getByText(/数字每天北京时间 8:00 重置/)).toBeVisible();
  // 导航计数：用户、档位、可用邀请码（概览加载这三组数据）。
  expect(within(console).getByRole("button", { name: "用户 1" })).toBeInTheDocument();
  expect(within(console).getByRole("button", { name: "档位 2" })).toBeInTheDocument();
  expect(within(console).getByRole("button", { name: "邀请码 2" })).toBeInTheDocument();
  // 邀请码卡片的「管理邀请码」直达邀请码分区。
  fireEvent.click(within(console).getByRole("button", { name: "管理邀请码" }));
  await waitFor(() => expect(within(console).getByRole("table", { name: "邀请码列表" })).toBeInTheDocument());
  // 档位人数行可点：回概览后直达档位分区并选中该档。
  fireEvent.click(within(console).getByRole("button", { name: "概览" }));
  await waitFor(() => expect(within(console).getByText("今日 token")).toBeInTheDocument());
  fireEvent.click(within(console).getByRole("button", { name: /基础档/ }));
  await waitFor(() => expect(within(console).getByRole("combobox", { name: "基础档 每日 token" })).toBeInTheDocument());
});

test("用户分区：档位/状态下拉即时保存，来源与绑定账户入列", async () => {
  const runtime = adminRuntime({ role: "owner" });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  await waitFor(() => expect(within(console).getByText("今日 token")).toBeInTheDocument());
  fireEvent.click(within(console).getByRole("button", { name: "用户 1" }));
  const table = await within(console).findByRole("table", { name: "用户列表" });
  // 来源 humanize：invite-code → 邀请码注册；绑定交易账户来自授权反查。
  expect(table).toHaveTextContent("邀请码注册");
  expect(table).toHaveTextContent("TqKq 模拟账户");
  // 档位/状态是下拉，改动即时 PUT，行内显示保存结果。
  fireEvent.change(within(console).getByRole("combobox", { name: "u1 档位" }), { target: { value: "pro" } });
  await waitFor(() => expect(runtime.calls.find(call => call.path === "/api/admin/users/u1")?.body).toEqual({ plan: "pro" }));
  await waitFor(() => expect(within(console).getAllByText("已保存").length).toBeGreaterThan(0));
  fireEvent.change(within(console).getByRole("combobox", { name: "u1 状态" }), { target: { value: "disabled" } });
  await waitFor(() => expect(runtime.calls.filter(call => call.path === "/api/admin/users/u1")[1].body).toEqual({ disabled: true }));
});

test("邀请码分区：四选项生成、复制、不限次数展开、筛选、停用、旧码不可见、表单内报错", async () => {
  const runtime = adminRuntime({ role: "owner" });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(await within(console).findByRole("button", { name: "邀请码 2" }));
  let table = await within(console).findByRole("table", { name: "邀请码列表" });
  // 默认只看可用：两个 active 在列，过期与旧码不在。
  expect(table).toHaveTextContent("GATE-2026");
  expect(table).toHaveTextContent("open-door");
  expect(table).not.toHaveTextContent("已过期");
  expect(table).not.toHaveTextContent("旧码不可见");
  // 不限次数的「N 人」可展开：明细一行一个使用者。
  fireEvent.click(within(table).getByRole("button", { name: "2 人" }));
  expect(table).toHaveTextContent("u2");
  expect(table).toHaveTextContent("u3");
  fireEvent.click(within(table).getByRole("button", { name: "2 人" }));
  // 切到全部：旧码只显示「旧码不可见」，过期/已用完可见。
  fireEvent.click(within(console).getByRole("radio", { name: "全部" }));
  table = within(console).getByRole("table", { name: "邀请码列表" });
  expect(table).toHaveTextContent("旧码不可见");
  expect(table).toHaveTextContent("已过期");
  expect(table).toHaveTextContent("已用完");
  // 生成表单：四个选项都进请求体；自定义码区分大小写原样上送。
  fireEvent.click(within(console).getByRole("button", { name: "生成邀请码" }));
  fireEvent.change(within(console).getByRole("textbox", { name: "邀请码" }), { target: { value: "TEAM-2026" } });
  fireEvent.click(within(within(console).getByRole("radiogroup", { name: "可用次数" })).getByRole("radio", { name: "不限次数" }));
  fireEvent.change(within(console).getByRole("combobox", { name: "档位" }), { target: { value: "pro" } });
  fireEvent.change(within(console).getByRole("combobox", { name: "有效期" }), { target: { value: "7" } });
  fireEvent.click(within(console).getByRole("button", { name: "生成" }));
  await waitFor(() => expect(runtime.calls.find(call => call.path === "/api/auth/owner/invite-codes" && call.method === "POST")?.body)
    .toEqual({ daysValid: 7, plan: "pro", code: "TEAM-2026", uses: "unlimited" }));
  // 成功后表单收起，横幅显示新码并带复制按钮。
  await waitFor(() => expect(within(console).queryByRole("textbox", { name: "邀请码" })).toBeNull());
  await waitFor(() => expect(within(console).getAllByText("TEAM-2026").length).toBeGreaterThan(0));
  // 长期有效：neverExpires 上送，daysValid 回到默认 14。
  fireEvent.click(within(console).getByRole("button", { name: "生成邀请码" }));
  fireEvent.change(within(console).getByRole("textbox", { name: "邀请码" }), { target: { value: "evergreen" } });
  fireEvent.change(within(console).getByRole("combobox", { name: "有效期" }), { target: { value: "never" } });
  fireEvent.click(within(console).getByRole("button", { name: "生成" }));
  await waitFor(() => expect(runtime.calls.filter(call => call.path === "/api/auth/owner/invite-codes" && call.method === "POST")[1].body)
    .toEqual({ daysValid: 14, plan: "basic", code: "evergreen", uses: "once", neverExpires: true }));
  // 长期有效的行：有效至显示「长期」（横幅与行里各出现一次）。
  const table2 = within(console).getByRole("table", { name: "邀请码列表" });
  await waitFor(() => expect(within(table2).getByText("evergreen")).toBeInTheDocument());
  const evergreenRow = within(table2).getByText("evergreen").closest("tr")!;
  expect(evergreenRow).toHaveTextContent("长期");
  // 复制：横幅按钮走剪贴板，列表行的 ⧉ 也走剪贴板。
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(window.navigator, "clipboard", { value: { writeText }, configurable: true });
  fireEvent.click(within(console).getByRole("button", { name: "复制" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("evergreen"));
  fireEvent.click(within(console).getByRole("button", { name: "复制 GATE-2026" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("GATE-2026"));
  // 表单内报错：422 格式、409 已存在，表单保持打开。
  runtime.failNextInviteWith(422);
  fireEvent.click(within(console).getByRole("button", { name: "生成邀请码" }));
  fireEvent.change(within(console).getByRole("textbox", { name: "邀请码" }), { target: { value: "坏" } });
  fireEvent.click(within(console).getByRole("button", { name: "生成" }));
  await waitFor(() => expect(within(console).getByRole("alert")).toHaveTextContent("邀请码格式不对"));
  runtime.failNextInviteWith(409);
  fireEvent.click(within(console).getByRole("button", { name: "生成" }));
  await waitFor(() => expect(within(console).getByRole("alert")).toHaveTextContent("这个邀请码已经存在"));
  fireEvent.click(within(console).getByRole("button", { name: "取消" }));
  // 停用：确认后行变为已停用，操作格消失。
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  const gateRow = within(console).getByText("GATE-2026").closest("tr")!;
  fireEvent.click(within(gateRow).getByRole("button", { name: "停用" }));
  await waitFor(() => expect(runtime.calls.some(call => call.path === "/api/auth/owner/invite-codes/inv-2/disable" && call.method === "POST")).toBe(true));
  const disabledRow = within(console).getByText("GATE-2026").closest("tr")!;
  await waitFor(() => expect(disabledRow).toHaveTextContent("已停用"));
  expect(within(disabledRow).queryByRole("button", { name: "停用" })).toBeNull();
  confirm.mockRestore();
});

test("档位分区：左列表右编辑器、预设与自定义额度、新建复制、删除空档、跳转技能", async () => {
  const runtime = adminRuntime({ role: "owner" });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(await within(console).findByRole("button", { name: "档位 2" }));
  await waitFor(() => expect(within(console).getByRole("combobox", { name: "基础档 每日 token" })).toBeInTheDocument());
  // 左列是档位卡（默认档带标签），右列是编辑器。
  const list = within(console).getByRole("group", { name: "档位列表" });
  expect(within(within(list).getByRole("button", { name: /基础档/ })).getByText("默认")).toBeInTheDocument();
  // token：预设下拉 + 自定义数字框，请求体只带点的那一项。
  const tokenSelect = within(console).getByRole("combobox", { name: "基础档 每日 token" });
  expect(tokenSelect).toHaveValue("300000");
  expect(within(tokenSelect).getAllByRole("option").map(option => option.textContent))
    .toEqual(["不限", "5万", "10万", "30万", "50万", "100万", "200万", "500万", "自定义…"]);
  fireEvent.change(tokenSelect, { target: { value: "100000" } });
  await waitFor(() => expect(runtime.calls.find(call => call.path === "/api/admin/plans/basic")?.body).toEqual({ dailyTokens: 100_000 }));
  fireEvent.change(tokenSelect, { target: { value: "unlimited" } });
  await waitFor(() => expect(runtime.calls.filter(call => call.path === "/api/admin/plans/basic")[1].body).toEqual({ dailyTokens: null }));
  fireEvent.change(tokenSelect, { target: { value: "custom" } });
  fireEvent.change(within(console).getByRole("spinbutton", { name: "基础档 自定义每日 token" }), { target: { value: "12345" } });
  await waitFor(() => expect(runtime.calls.filter(call => call.path === "/api/admin/plans/basic")[2].body).toEqual({ dailyTokens: 12_345 }));
  // 语音同理（秒存分钟显）。
  const voiceSelect = within(console).getByRole("combobox", { name: "基础档 每日语音" });
  expect(voiceSelect).toHaveValue("1800");
  fireEvent.change(voiceSelect, { target: { value: "0" } });
  await waitFor(() => expect(runtime.calls.filter(call => call.path === "/api/admin/plans/basic")[3].body).toEqual({ voiceSecondsPerDay: 0 }));
  fireEvent.change(voiceSelect, { target: { value: "custom" } });
  fireEvent.change(within(console).getByRole("spinbutton", { name: "基础档 自定义每日语音分钟" }), { target: { value: "45" } });
  await waitFor(() => expect(runtime.calls.filter(call => call.path === "/api/admin/plans/basic")[4].body).toEqual({ voiceSecondsPerDay: 2_700 }));
  // AI 简报品种上限（briefProducts）：数字框，请求体只带这一项。
  const briefCap = within(console).getByRole("spinbutton", { name: "基础档 AI 简报品种上限" });
  expect(briefCap).toHaveValue(3);
  expect(within(console).getByText("管理员不限；调低后超出的只取前 N 个")).toBeVisible();
  fireEvent.change(briefCap, { target: { value: "5" } });
  await waitFor(() => expect(runtime.calls.filter(call => call.path === "/api/admin/plans/basic")[5].body).toEqual({ briefProducts: 5 }));
  // 功能开关已移入「技能」分区：这里只有提示与跳转按钮。
  expect(within(console).queryByRole("switch", { name: "基础档 回测" })).toBeNull();
  expect(within(console).getByText("功能开关在「技能」分区设置")).toBeVisible();
  // 新建档位：表单在左列顶部，创建走 POST（复制来源默认默认档）。
  fireEvent.click(within(console).getByRole("button", { name: "新建档位" }));
  fireEvent.change(within(console).getByRole("textbox", { name: "新档位显示名称" }), { target: { value: "内测档" } });
  fireEvent.click(within(console).getByRole("button", { name: "创建" }));
  await waitFor(() => expect(runtime.calls.find(call => call.path === "/api/admin/plans" && call.method === "POST")?.body)
    .toEqual({ label: "内测档", copyFrom: "basic" }));
  await waitFor(() => expect(within(console).getByRole("group", { name: "档位列表" })).toHaveTextContent("内测档"));
  // 选中高级档：空档可删（默认档/在用档禁用并带原因）。
  fireEvent.click(within(console).getByRole("button", { name: /高级档/ }));
  await waitFor(() => expect(within(console).getByRole("combobox", { name: "高级档 每日 token" })).toBeInTheDocument());
  fireEvent.click(within(console).getByRole("button", { name: "删除档位" }));
  await waitFor(() => expect(runtime.calls.some(call => call.path === "/api/admin/plans/pro" && call.method === "DELETE")).toBe(true));
  // 跳转按钮直达技能矩阵。
  fireEvent.click(within(console).getByRole("button", { name: "前往「技能」分区" }));
  await waitFor(() => expect(within(console).getByRole("table", { name: "技能开关矩阵" })).toBeInTheDocument());
  // 交易账户：只读核对。
  fireEvent.click(within(console).getByRole("button", { name: "交易账户" }));
  const accountsTable = await within(console).findByRole("table", { name: "交易账户与授权" });
  expect(accountsTable).toHaveTextContent("快期模拟");
  expect(accountsTable).toHaveTextContent("boss（所有者）");
  expect(accountsTable).toHaveTextContent("u1（只读）");
});

test("技能分区矩阵：按分类分组、「—」列、全局/档位写请求体、失败回滚、停用 admin-console 二次确认", async () => {
  const runtime = adminRuntime({ role: "owner", registry: true });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(within(console).getByRole("button", { name: "技能" }));
  const matrix = await within(console).findByRole("table", { name: "技能开关矩阵" });
  // 分组：注册表按 id 列出、同分类不相邻，矩阵仍每个分类只有一行组头，按产品顺序排列。
  const groupHeads = matrix.querySelectorAll("tr.fdw-admin-skill-group");
  expect([...groupHeads].map(row => row.textContent)).toEqual(["交易", "策略", "系统"]);
  expect(within(matrix).getByText("让 AI 在你设定的品种、手数、笔数和时长内自己下模拟单")).toBeInTheDocument();
  // The rule line is behind the section title's "?", not a cell inside the table.
  fireEvent.click(within(console).getByRole("button", { name: "技能的说明" }));
  expect(screen.getByRole("tooltip")).toHaveTextContent("全局对所有人生效（含主账号）；档位列只对该档位成员生效；“—”表示不分档位或不可设；下级技能跟随上级。");
  // tierGated=false 或 owner-only 的技能：每个档位一格「—」，没有开关可勾。
  const adminRow = within(matrix).getByText("后台管理").closest("tr")!;
  expect(within(adminRow).getAllByLabelText("不适用")).toHaveLength(2);
  const helpRow = within(matrix).getByText("使用说明").closest("tr")!;
  expect(within(helpRow).getAllByLabelText("不适用")).toHaveLength(2);
  // 下级技能默认收起：上级行有「含 N 项」展开按钮，展开后子行才渲染。
  const expand = within(matrix).getByRole("button", { name: /含 1 项/ });
  expect(expand).toHaveAttribute("aria-expanded", "false");
  expect(within(matrix).queryByRole("switch", { name: "策略信号解读 全局" })).toBeNull();
  fireEvent.click(expand);
  expect(expand).toHaveAttribute("aria-expanded", "true");
  const signalsRow = within(matrix).getByText("策略信号解读").closest("tr")!;
  expect(signalsRow.className).toContain("fdw-admin-skill-child");
  // 全局启停与档位勾选是两条不同的即时保存通道，请求体只带点的那一项。
  fireEvent.click(within(matrix).getByRole("switch", { name: "策略信号解读 全局" }));
  await waitFor(() => expect(runtime.calls.find(call => call.path === "/api/admin/skills/strategy-signals")?.body).toEqual({ enabled: false }));
  await waitFor(() => expect(within(matrix).getByText("已保存")).toBeInTheDocument());
  fireEvent.click(within(matrix).getByRole("switch", { name: "AI 自主执行 基础档" }));
  await waitFor(() => expect(runtime.calls.find(call => call.path === "/api/admin/plans/basic" && call.body?.skills)?.body)
    .toEqual({ skills: { "ai-execution": true } }));
  // 写失败：乐观翻转弹回原值，行旁给出错误行。
  runtime.failSkillWrites(true);
  fireEvent.click(within(matrix).getByRole("switch", { name: "策略信号解读 全局" }));
  await waitFor(() => expect(within(matrix).getByText("保存未生效，已恢复")).toBeInTheDocument());
  expect(within(matrix).getByRole("switch", { name: "策略信号解读 全局" })).toHaveAttribute("aria-checked", "false");
  runtime.failSkillWrites(false);
  // 停用 admin-console 先弹二次确认：取消不写库，确认才 PUT。
  fireEvent.click(within(matrix).getByRole("switch", { name: "后台管理 全局" }));
  const sheet = screen.getByRole("dialog", { name: "停用「后台管理」技能" });
  // The switch only takes 后台管理 away from the AI (owner 2026-09-27); the owner's own page keeps working.
  expect(sheet).toHaveTextContent("停用后 AI（内置与外部）不能再调用后台管理接口；你在本页的操作不受影响，技能分区仍可重新启用");
  fireEvent.click(within(sheet).getByRole("button", { name: "暂不停用" }));
  expect(runtime.calls.some(call => call.path === "/api/admin/skills/admin-console" && call.method === "PUT")).toBe(false);
  fireEvent.click(within(matrix).getByRole("switch", { name: "后台管理 全局" }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "停用「后台管理」技能" })).getByRole("button", { name: "确认停用" }));
  await waitFor(() => expect(runtime.calls.find(call => call.path === "/api/admin/skills/admin-console")?.body).toEqual({ enabled: false }));
});

test("技能分区矩阵：上级全局关闭时子技能开关禁用并提示跟随上级", async () => {
  const runtime = adminRuntime({ role: "owner", registry: true });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  // 全局关掉上级 strategy-management。
  fireEvent.click(within(console).getByRole("button", { name: "技能" }));
  const matrix = await within(console).findByRole("table", { name: "技能开关矩阵" });
  fireEvent.click(within(matrix).getByRole("switch", { name: "策略管理与监控 全局" }));
  await waitFor(() => expect(within(matrix).getByText("已保存")).toBeInTheDocument());
  // 展开子行：子行的全局与档位开关都禁用，summary 下提示随上级关闭。
  fireEvent.click(within(matrix).getByRole("button", { name: /含 1 项/ }));
  const signalsRow = within(matrix).getByText("策略信号解读").closest("tr")!;
  expect(within(signalsRow).getByText("随「策略管理与监控」关闭")).toBeInTheDocument();
  expect(within(signalsRow).getByRole("switch", { name: "策略信号解读 全局" })).toBeDisabled();
  expect(within(signalsRow).getByRole("switch", { name: "策略信号解读 基础档" })).toBeDisabled();
  // 档位没开上级时该档位的子开关同样禁用（上级开着、basic 没勾）。
  const managementRow = within(matrix).getByText("策略管理与监控").closest("tr")!;
  fireEvent.click(within(managementRow).getByRole("switch", { name: "策略管理与监控 全局" }));
  // Wait for the second global write to finish (busy clears only after its
  // response has replaced the skill list), so it cannot clobber the tier write below.
  await waitFor(() => expect(within(managementRow).getByRole("switch", { name: "策略管理与监控 全局" })).toBeDisabled());
  await waitFor(() => expect(within(managementRow).getByRole("switch", { name: "策略管理与监控 全局" })).toBeEnabled());
  await waitFor(() => expect(runtime.calls.filter(call => call.path === "/api/admin/skills/strategy-management").length).toBe(2));
  fireEvent.click(within(matrix).getByRole("switch", { name: "策略管理与监控 基础档" }));
  await waitFor(() => expect(runtime.calls.find(call => call.path === "/api/admin/plans/basic" && call.body?.skills)?.body)
    .toEqual({ skills: { "strategy-management": false } }));
  expect(within(signalsRow).getByRole("switch", { name: "策略信号解读 基础档" })).toBeDisabled();
  expect(within(signalsRow).getByRole("switch", { name: "策略信号解读 高级档" })).toBeEnabled();
});

test("游客档管理：保存行情范围，保留身份不能删除或分配给注册用户", async () => {
  const runtime = adminRuntime({ role: "owner", registry: true, guest: true });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(await within(console).findByRole("button", { name: "档位 3" }));
  await waitFor(() => expect(within(console).getByRole("combobox", { name: "基础档 每日 token" })).toBeInTheDocument());
  fireEvent.click(within(console).getByRole("button", { name: /游客/ }));
  const history = await within(console).findByRole("combobox", { name: "游客 行情交易日" });
  expect(history).toHaveValue("3");
  // 游客是保留身份档：删除禁用带原因，模型开关禁用，不消耗 AI 额度。
  const deleteButton = within(console).getByRole("button", { name: "删除档位" });
  expect(deleteButton).toBeDisabled();
  expect(deleteButton).toHaveAttribute("title", "游客是保留身份档，不能删除");
  expect(within(console).getByRole("switch", { name: "游客 DeepSeek Flash" })).toBeDisabled();
  expect(within(console).queryByRole("combobox", { name: "游客 每日 token" })).toBeNull();
  expect(within(console).getByRole("combobox", { name: "游客 每日语音" })).toHaveValue("60");
  fireEvent.change(history, { target: { value: "1" } });
  await waitFor(() => expect(runtime.calls.find(call => call.path === "/api/admin/plans/guest")?.body).toEqual({ marketHistoryTradingDays: 1 }));
  // 注册用户与邀请码都不能落在游客档；技能矩阵游客列只有语音。
  fireEvent.click(within(console).getByRole("button", { name: "用户 1" }));
  const userPlan = await within(console).findByRole("combobox", { name: "u1 档位" });
  expect(within(userPlan).queryByRole("option", { name: "游客" })).toBeNull();
  fireEvent.click(within(console).getByRole("button", { name: "邀请码 2" }));
  fireEvent.click(within(console).getByRole("button", { name: "生成邀请码" }));
  await within(console).findByRole("combobox", { name: "有效期" });
  expect(within(within(console).getByRole("combobox", { name: "档位" })).queryByRole("option", { name: "游客" })).toBeNull();
  fireEvent.click(within(console).getByRole("button", { name: "技能" }));
  await within(console).findByRole("table", { name: "技能开关矩阵" });
  expect(within(console).queryByRole("switch", { name: "AI 自主执行 游客" })).toBeNull();
});

test("技能矩阵连点两格：先发的那格失败回滚只恢复它自己，不把后保存成功的那格翻回去", async () => {
  adminRuntime({ role: "owner", registry: true });
  const base = globalThis.fetch;
  let failBasic: () => void = () => undefined;
  vi.stubGlobal("fetch", (input: Request | string, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    if (request.method === "PUT" && new URL(request.url).pathname === "/api/admin/plans/basic")
      return new Promise<Response>(resolve => { failBasic = () => resolve(new Response("boom", { status: 500 })); });
    return base(input, init);
  });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(within(console).getByRole("button", { name: "技能" }));
  const matrix = await within(console).findByRole("table", { name: "技能开关矩阵" });
  fireEvent.click(within(matrix).getByRole("switch", { name: "AI 自主执行 基础档" }));   // 关→开，迟迟不回
  fireEvent.click(within(matrix).getByRole("switch", { name: "AI 自主执行 高级档" }));   // 开→关，保存成功
  await waitFor(() => expect(within(matrix).getByText("已保存")).toBeInTheDocument());
  await act(async () => { failBasic(); });
  await waitFor(() => expect(within(matrix).getByText("保存未生效，已恢复")).toBeInTheDocument());
  expect(within(matrix).getByRole("switch", { name: "AI 自主执行 基础档" })).toHaveAttribute("aria-checked", "false");
  expect(within(matrix).getByRole("switch", { name: "AI 自主执行 高级档" })).toHaveAttribute("aria-checked", "false");
});

test("新建档位成功但列表刷新失败：说已新建，不提示“未生效”引人重试建出重复档位", async () => {
  const runtime = adminRuntime({ role: "owner" });
  const base = globalThis.fetch;
  let created = false;
  vi.stubGlobal("fetch", async (input: Request | string, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    if (created && request.method === "GET" && path === "/api/admin/plans") return new Response("boom", { status: 502 });
    const response = await base(input, init);
    if (request.method === "POST" && path === "/api/admin/plans") created = true;
    return response;
  });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(await within(console).findByRole("button", { name: "档位 2" }));
  await waitFor(() => expect(within(console).getByRole("combobox", { name: "基础档 每日 token" })).toBeInTheDocument());
  fireEvent.click(within(console).getByRole("button", { name: "新建档位" }));
  fireEvent.change(within(console).getByRole("textbox", { name: "新档位显示名称" }), { target: { value: "内测档" } });
  fireEvent.click(within(console).getByRole("button", { name: "创建" }));
  expect(await within(console).findByText("已新建档位；列表没刷新出来，切换分区再看")).toBeVisible();
  expect(within(console).queryByText("新建档位未生效，请重试")).toBeNull();
  expect(runtime.calls.filter(call => call.path === "/api/admin/plans" && call.method === "POST")).toHaveLength(1);
});

/** The 服务器 and 行情源 interfaces layered over `adminRuntime`: each handler answers its
 *  path, every call is recorded with its query and body. */
function withAdminInfra(handlers: {
  servers?: (range: string) => Response;
  rum?: (range: string) => Response;
  marketSources?: (method: string, body: { accountIds: string[] } | undefined) => Response;
  marketSourceAccounts?: (method: string, path: string, body: { username: string; password: string } | undefined) => Response;
}) {
  const base = globalThis.fetch;
  const seen: Array<{ method: string; path: string; range: string | null; body?: unknown }> = [];
  vi.stubGlobal("fetch", async (input: Request | string, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const url = new URL(request.url);
    if (url.pathname === "/api/admin/servers" && handlers.servers) {
      seen.push({ method: request.method, path: url.pathname, range: url.searchParams.get("range") });
      return handlers.servers(url.searchParams.get("range") ?? "");
    }
    if (url.pathname === "/api/admin/rum" && handlers.rum) {
      seen.push({ method: request.method, path: url.pathname, range: url.searchParams.get("range") });
      return handlers.rum(url.searchParams.get("range") ?? "");
    }
    if (url.pathname === "/api/admin/market-sources" && handlers.marketSources) {
      const body = request.method === "PUT" ? JSON.parse(await request.text()) as { accountIds: string[] } : undefined;
      seen.push({ method: request.method, path: url.pathname, range: null, body });
      return handlers.marketSources(request.method, body);
    }
    if (url.pathname.startsWith("/api/admin/market-sources/") && handlers.marketSourceAccounts) {
      const body = request.method === "POST" ? JSON.parse(await request.text()) as { username: string; password: string } : undefined;
      seen.push({ method: request.method, path: url.pathname, range: null, body });
      return handlers.marketSourceAccounts(request.method, url.pathname, body);
    }
    return base(input, init);
  });
  return seen;
}

const jsonResponse = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

function serverHost(id: string, name: string, overrides: Record<string, unknown> = {}) {
  return {
    id, displayName: name, platform: "Ubuntu", role: `${name} 的角色`, checkedAt: "2026-10-04T07:58:00Z", reachability: "online",
    resources: { cpuCores: 8, cpuPercent: 41.5, load1: 0.84, load5: 0.9, load15: 0.95, memTotalMB: 16000, memUsedMB: 8000,
      diskTotalGB: 100, diskUsedGB: 40, uptimeSeconds: 864000, containers: 3 },
    series: [{ at: "2026-10-04T07:00:00Z", cpu: 40 }, { at: "2026-10-04T07:05:00Z", cpu: 42 }],
    servicesSummary: { total: 2, running: 1, failed: 1 },
    ...overrides,
  };
}

const serversOk = (extra: Record<string, unknown> = {}) => ({
  status: "ok", updatedAt: "2026-10-04T08:00:00Z", fetchedAt: "2026-10-04T08:00:30+00:00",
  hosts: [
    serverHost("tencent-ai-trader", "主机甲"),
    serverHost("gpu-4090d", "4090D 工作站", {
      platform: "Windows",
      resources: { cpuCores: 24, cpuPercent: 6, memTotalMB: 32768, memUsedMB: 27852, diskTotalGB: 500, diskUsedGB: 325,
        disks: [{ name: "C:", totalGB: 500, usedGB: 325 }, { name: "D:", totalGB: 1000, usedGB: 800 }] },
      servicesSummary: { total: 1, running: 1, failed: 0 },
    }),
    serverHost("nas-ci", "家中 NAS", { reachability: "unknown", servicesSummary: { total: 0, running: 0, failed: 0 } }),
  ],
  ...extra,
});

test("后台导航：四个分组、分区切换，新分区服务器与行情源可达", async () => {
  adminRuntime({ role: "owner" });
  withAdminInfra({
    servers: () => jsonResponse({ status: "unconfigured" }),
    marketSources: () => jsonResponse({ items: [], seriesPerSource: 48 }),
  });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  const nav = within(console).getByRole("navigation", { name: "后台管理分区导航" });
  // 分组顺序与成员：运营 · 能力 · 交易 · 基础设施。
  const groups = within(nav).getAllByRole("group");
  expect(groups.map(group => group.getAttribute("aria-label"))).toEqual(["运营", "能力", "交易", "基础设施"]);
  expect(within(groups[0]).getAllByRole("button").map(button => button.textContent?.replace(/\d+$/, ""))).toEqual(["概览", "用户", "档位", "邀请码"]);
  expect(within(groups[2]).getAllByRole("button").map(button => button.textContent)).toEqual(["交易账户", "行情源"]);
  expect(within(nav).getByRole("button", { name: "概览" })).toHaveAttribute("aria-current", "true");
  // 侧栏底部：当前管理员与两个页面入口。
  expect(screen.getByText("boss")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "退出登录" })).toBeInTheDocument();
  fireEvent.click(within(nav).getByRole("button", { name: "服务器" }));
  expect(await within(console).findByText("运维台令牌未配置")).toBeInTheDocument();
  expect(within(nav).getByRole("button", { name: "服务器" })).toHaveAttribute("aria-current", "true");
  expect(within(console).getByRole("heading", { name: "服务器" })).toBeInTheDocument();
  fireEvent.click(within(nav).getByRole("button", { name: "行情源" }));
  expect(await within(console).findByText("清单为空：游客行情没有来源，先加一个快期账号")).toBeInTheDocument();
  expect(within(console).getByRole("heading", { name: "行情源" })).toBeInTheDocument();
});

const rumOk = (extra: Record<string, unknown> = {}) => ({
  status: "ok", enabled: true, sampleRate: 1, range: "24h", slotSeconds: 600, from: 1_800_000_000, to: 1_800_086_400, retentionDays: 14,
  totals: { sessions: 5, users: 2, visibleMinutes: 312.4, byClient: { web: { sessions: 3, users: 2 }, desktop: { sessions: 2, users: 1 } },
    byNetwork: { wifi: { sessions: 4 }, unknown: { sessions: 1 } } },
  metrics: {
    lcp: { unit: "ms", n: 5, p50: 900, p75: 1240, p95: 2100, byClient: { web: { n: 3, p50: 950, p75: 1300, p95: 2200 } },
      series: [{ t: 1_800_000_000, n: 5, p50: 900, p95: 2100 }] },
    price_latency: { unit: "ms", n: 400, p50: 180, p75: 260, p95: 640,
      byClient: { web: { n: 300, p50: 170, p75: 250, p95: 600 }, desktop: { n: 100, p50: 200, p75: 280, p95: 700 } },
      series: [{ t: 1_800_000_000, n: 200, p50: 170, p95: 600 }, { t: 1_800_000_600, n: 200, p50: 190, p95: 680 }] },
  },
  counters: {
    sseReconnect: { total: 3, byClient: { web: 3 }, series: [] },
    errors: { total: 4, byCategory: { network: 3, type: 1 }, byClient: { web: 4 }, series: [] },
  },
  ...extra,
});

test("用户体验页：分位数卡片、按客户端分列、报错类别、范围切换即重读、采集关闭如实标注", async () => {
  adminRuntime({ role: "owner" });
  let enabled = true;
  const seen = withAdminInfra({ rum: () => jsonResponse(rumOk({ enabled })) });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  const nav = within(console).getByRole("navigation", { name: "后台管理分区导航" });
  fireEvent.click(within(nav).getByRole("button", { name: "用户体验" }));
  expect(await within(console).findByLabelText("用户体验")).toHaveTextContent("会话5");
  expect(within(console).getByText("1240 ms")).toBeInTheDocument();          // 页面打开 p75
  expect(within(console).getByText("640 ms")).toBeInTheDocument();           // 价格延迟 p95
  const table = within(console).getByRole("table", { name: "按客户端" });
  const latency = within(table).getByRole("row", { name: /价格延迟/ });
  expect(within(latency).getAllByRole("cell").map(cell => cell.textContent)).toEqual(["170\u00a0ms / 600\u00a0ms", "200\u00a0ms / 700\u00a0ms", "—"]);
  expect(within(console).getByRole("region", { name: "报错类别" })).toHaveTextContent("网络请求3");
  expect(within(console).getByRole("region", { name: "网络类型" })).toHaveTextContent("Wi-Fi4");
  // The default metric (价格延迟) is drawn: two p50 points.
  expect(chartData.some(data => Array.isArray(data) && data.length === 2)).toBe(true);
  fireEvent.click(within(console).getByRole("radio", { name: "7 天" }));
  await waitFor(() => expect(seen.map(call => call.range)).toContain("7d"));
  enabled = false;
  fireEvent.click(within(console).getByRole("radio", { name: "1 小时" }));
  expect(await within(console).findByText("采集已关闭，以下是关闭前的数据")).toBeInTheDocument();
});

test("用户体验页：采样率低于 1 时常显采样率，落在最后一档的分位数标“≥”", async () => {
  adminRuntime({ role: "owner" });
  withAdminInfra({ rum: () => jsonResponse(rumOk({
    sampleRate: 0.25,
    metrics: {
      sse_down: { unit: "ms", n: 4, p50: 60000, p75: 60000, p95: 60000, atLeast: ["p50", "p75", "p95"],
        byClient: { web: { n: 4, p50: 60000, p75: 60000, p95: 60000, atLeast: ["p50", "p75", "p95"] } }, series: [] },
    },
  })) });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  const nav = within(console).getByRole("navigation", { name: "后台管理分区导航" });
  fireEvent.click(within(nav).getByRole("button", { name: "用户体验" }));
  expect(await within(console).findByText(/仅采样 25% 的页面会话/)).toBeInTheDocument();
  expect(within(console).getByText(/断线 p95 ≥ 60\.0 秒/)).toBeInTheDocument();
  const table = within(console).getByRole("table", { name: "按客户端" });
  const down = within(table).getByRole("row", { name: /断线时长/ });
  expect(within(down).getAllByRole("cell")[0].textContent).toBe("≥\u00a060.0\u00a0秒 / ≥\u00a060.0\u00a0秒");
});

test("服务器页：仪表阈值配色、Windows 其他盘、服务胶囊、未采集变暗、范围切换即重读", async () => {
  adminRuntime({ role: "owner" });
  const seen = withAdminInfra({ servers: () => jsonResponse(serversOk()) });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(within(console).getByRole("button", { name: "服务器" }));
  const hosts = await within(console).findByRole("list", { name: "主机" });
  // 默认 24 小时（概览的系统块也读 24 小时）。
  expect(seen.length).toBeGreaterThan(0);
  expect(seen.every(call => call.range === "24h")).toBe(true);
  // 摘要：在线 2 台（unknown 不算在线）、未采集 1、告警 2（主机甲有失败服务，4090D 内存 ≥80%）。
  expect(within(console).getByRole("status")).toHaveTextContent("2 台在线 · 1 台未采集 · 2 告警");
  // 仪表：<60% 常态色，≥60% 警告，≥80% 危险。
  const first = within(hosts).getByRole("listitem", { name: "主机甲" });
  expect(within(first).getByRole("img", { name: "CPU 42%" })).toHaveAttribute("data-level", "normal");
  expect(within(first).getByRole("img", { name: "内存 50%" })).toHaveAttribute("data-level", "normal");
  expect(first).toHaveAttribute("data-state", "alert");
  expect(within(first).getByText("运行 1")).toBeInTheDocument();
  expect(within(first).getByText("失败 1")).toBeInTheDocument();
  expect(first).toHaveTextContent("8 核 · 15.6 GB · 100 GB");
  expect(first).toHaveTextContent("运行 10 天");
  const windows = within(hosts).getByRole("listitem", { name: "4090D 工作站" });
  expect(within(windows).getByRole("img", { name: "内存 85%" })).toHaveAttribute("data-level", "danger");
  expect(within(windows).getByRole("img", { name: "磁盘 65%" })).toHaveAttribute("data-level", "warn");
  // 磁盘仪表取系统盘（C:），其他盘小字列出。
  expect(windows).toHaveTextContent("D: 800/1000 GB");
  expect(windows).not.toHaveTextContent("C: 325/500 GB");
  // reachability=unknown：卡片变暗并标未采集。
  const nas = within(hosts).getByRole("listitem", { name: "家中 NAS" });
  expect(nas).toHaveAttribute("data-state", "idle");
  expect(within(nas).getByText("未采集")).toBeInTheDocument();
  // 链路图节点取对应主机的状态。
  expect(within(console).getByRole("region", { name: "请求与发布链路" })).toHaveTextContent("Runtime · 模拟引擎");
  // 切换范围立即按新范围重读，曲线标题跟着变。
  fireEvent.click(within(console).getByRole("radio", { name: "1 小时" }));
  await waitFor(() => expect(seen.map(call => call.range)).toContain("1h"));
  expect(within(first).getByText("1 小时 CPU · 峰值 42%")).toBeInTheDocument();
  fireEvent.click(within(console).getByRole("radio", { name: "7 天" }));
  await waitFor(() => expect(seen.map(call => call.range)).toContain("7d"));
});

test("服务器页：运维台过期时摘要标明上次时间；读不到时说明会重试", async () => {
  adminRuntime({ role: "owner" });
  let mode: "stale" | "down" = "stale";
  withAdminInfra({ servers: () => mode === "stale" ? jsonResponse(serversOk({ stale: true })) : new Response("bad gateway", { status: 502 }) });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(within(console).getByRole("button", { name: "服务器" }));
  // fetchedAt 08:00:30Z = 北京时间 16:00。
  expect(await within(console).findByText("数据已过期（上次 16:00）")).toBeInTheDocument();
  mode = "down";
  fireEvent.click(within(console).getByRole("button", { name: "概览" }));
  fireEvent.click(within(console).getByRole("button", { name: "服务器" }));
  expect(await within(console).findByText("运维台暂时读不到，一分钟后自动重试")).toBeInTheDocument();
});

test("行情源页：上移/下移/停用整份顺序写入、失败回滚，添加只给启用中且不在清单的快期账户", async () => {
  adminRuntime({ role: "owner" });
  let order = ["pool-a", "pool-b"];
  let failWrites = false;
  const names: Record<string, string> = { "pool-a": "测试账号", "pool-b": "成员账号", "acc-1": "TqKq 模拟账户" };
  const snapshot = () => ({ seriesPerSource: 48, items: order.map((accountId, position) => ({
    accountId, position, providerKind: accountId === "acc-1" ? "tqkq-sidecar" : "tqkq-pool",
    health: accountId === "pool-b" ? "full" : "healthy", channels: accountId === "pool-a" ? 22 : 48, channelLimit: 48,
    quoteSymbols: accountId === "pool-a" ? 150 : 50, lastError: accountId === "pool-b" ? "market_subscription_limit" : null,
    lastErrorAt: accountId === "pool-b" ? "2026-10-04T06:00:00Z" : null, displayName: names[accountId], accountStatus: "active" })) });
  const seen = withAdminInfra({ marketSources: (method, body) => {
    if (method === "PUT") {
      if (failWrites) return new Response("boom", { status: 500 });
      order = body!.accountIds;
    }
    return jsonResponse(snapshot());
  } });
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(within(console).getByRole("button", { name: "行情源" }));
  const list = await within(console).findByRole("list", { name: "行情源清单" });
  const rows = () => within(list).getAllByRole("listitem").map(row => row.getAttribute("aria-label"));
  expect(rows()).toEqual(["1 测试账号", "2 成员账号"]);
  // 总量与每行：通道 70 / 96，报价占比，K 线方阵，最近错误。
  expect(within(console).getByLabelText("行情源总量")).toHaveTextContent("70 / 96");
  expect(within(list).getByRole("img", { name: "K 线占用 22 / 48" }).querySelectorAll("i[data-on]")).toHaveLength(22);
  expect(within(list).getByRole("listitem", { name: "2 成员账号" })).toHaveTextContent("最近错误：market_subscription_limit");
  expect(within(list).getByRole("button", { name: "上移 测试账号" })).toBeDisabled();
  // 下移：整份新顺序一次 PUT，界面立即按新顺序。
  fireEvent.click(within(list).getByRole("button", { name: "下移 测试账号" }));
  expect(rows()).toEqual(["1 成员账号", "2 测试账号"]);
  await waitFor(() => expect(seen.filter(call => call.method === "PUT").map(call => call.body)).toEqual([{ accountIds: ["pool-b", "pool-a"] }]));
  await waitFor(() => expect(within(console).getByText("已把「测试账号」下移")).toBeInTheDocument());
  fireEvent.click(within(list).getByRole("button", { name: "上移 测试账号" }));
  await waitFor(() => expect(seen.filter(call => call.method === "PUT")[1].body).toEqual({ accountIds: ["pool-a", "pool-b"] }));
  await waitFor(() => expect(within(list).getByRole("button", { name: "上移 成员账号" })).toBeEnabled());
  // 停用失败：先从清单消失，写入失败后回到原位并提示。
  failWrites = true;
  fireEvent.click(within(list).getByRole("button", { name: "停用 成员账号" }));
  expect(rows()).toEqual(["1 测试账号"]);
  await waitFor(() => expect(within(console).getByRole("alert")).toHaveTextContent("保存未生效，已恢复原顺序"));
  expect(rows()).toEqual(["1 测试账号", "2 成员账号"]);
  expect(seen.filter(call => call.method === "PUT")[2].body).toEqual({ accountIds: ["pool-a"] });
  expect(order).toEqual(["pool-a", "pool-b"]);
  // 添加：候选只有不在清单里的启用中快期账户（acc-1），加到末尾。
  failWrites = false;
  const picker = within(console).getByRole("combobox", { name: "选择快期账号" });
  expect(within(picker).getAllByRole("option").map(option => option.getAttribute("value"))).toEqual(["acc-1"]);
  fireEvent.click(within(console).getByRole("button", { name: "加入清单末尾" }));
  await waitFor(() => expect(seen.filter(call => call.method === "PUT")[3].body).toEqual({ accountIds: ["pool-a", "pool-b", "acc-1"] }));
  await waitFor(() => expect(rows()).toEqual(["1 测试账号", "2 成员账号", "3 TqKq 模拟账户"]));
  // 再没有可加的已有账户：那一行收起，只留输入快期账号与口令的添加方式。
  await waitFor(() => expect(within(console).queryByRole("combobox", { name: "选择快期账号" })).toBeNull());
  expect(within(console).queryByRole("button", { name: "加入清单末尾" })).toBeNull();
  expect(within(console).getByRole("textbox", { name: "快期账号" })).toBeInTheDocument();
});

test("行情源页：输入快期账号与口令验证后加到末尾，口令不回显；行情专用账号可移除", async () => {
  adminRuntime({ role: "owner" });
  let order = ["pool-a"];
  const marketOnly = new Set<string>();
  const snapshot = () => ({ seriesPerSource: 48, items: order.map((accountId, position) => ({
    accountId, position, providerKind: "tqkq-pool", health: "healthy", channels: 0, channelLimit: 48, quoteSymbols: 0,
    lastError: null, lastErrorAt: null, displayName: accountId === "pool-a" ? "测试账号" : "kq-new", accountStatus: "active",
    marketOnly: marketOnly.has(accountId) })) });
  const seen = withAdminInfra({
    marketSources: () => jsonResponse(snapshot()),
    marketSourceAccounts: (method, path, body) => {
      if (method === "POST") {
        if (body!.password !== "right-secret") return jsonResponse({ detail: { code: "tqkq_bind_failed", reason: "auth_failed" } }, 409);
        order = [...order, "tqkq-sim:new"];
        marketOnly.add("tqkq-sim:new");
        return jsonResponse({ accountId: "tqkq-sim:new", ...snapshot() });
      }
      order = order.filter(id => `/api/admin/market-sources/${encodeURIComponent(id)}` !== path);
      return jsonResponse(snapshot());
    },
  });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(within(console).getByRole("button", { name: "行情源" }));
  const list = await within(console).findByRole("list", { name: "行情源清单" });
  // 顶部主按钮把焦点放到账号输入框。
  fireEvent.click(within(console).getByRole("button", { name: "添加快期账号" }));
  const account = within(console).getByRole("textbox", { name: "快期账号" });
  expect(account).toHaveFocus();
  const secret = within(console).getByLabelText("快期口令");
  expect(secret).toHaveAttribute("type", "password");
  const submit = within(console).getByRole("button", { name: "验证并加入" });
  expect(submit).toBeDisabled();
  // 口令错：明确报错、不加入，口令框清空且不出现在页面文字里。
  fireEvent.change(account, { target: { value: "kq-new" } });
  fireEvent.change(secret, { target: { value: "wrong-secret" } });
  fireEvent.click(submit);
  await waitFor(() => expect(within(console).getByRole("alert")).toHaveTextContent("快期账号或口令不对，未添加"));
  expect(secret).toHaveValue("");
  expect(console.textContent).not.toContain("wrong-secret");
  expect(within(list).getAllByRole("listitem")).toHaveLength(1);
  // 口令对：加到末尾，标“仅行情”，账号与口令框都清空。
  fireEvent.change(secret, { target: { value: "right-secret" } });
  fireEvent.click(within(console).getByRole("button", { name: "验证并加入" }));
  await waitFor(() => expect(within(list).getAllByRole("listitem").map(row => row.getAttribute("aria-label"))).toEqual(["1 测试账号", "2 kq-new"]));
  expect(seen.find(call => call.method === "POST")).toMatchObject({ path: "/api/admin/market-sources/tqkq" });
  expect(within(console).getByText("已验证并把「kq-new」加到清单末尾，立即生效")).toBeInTheDocument();
  expect(within(list).getByRole("listitem", { name: "2 kq-new" })).toHaveTextContent("仅行情");
  expect(account).toHaveValue("");
  expect(secret).toHaveValue("");
  expect(console.textContent).not.toContain("right-secret");
  // 成员账户仍是“停用”；行情专用账号是“移除”，确认后 DELETE。
  expect(within(list).getByRole("button", { name: "停用 测试账号" })).toBeInTheDocument();
  fireEvent.click(within(list).getByRole("button", { name: "移除 kq-new" }));
  await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(1));
  expect(seen.find(call => call.method === "DELETE")).toMatchObject({ path: "/api/admin/market-sources/tqkq-sim%3Anew" });
  expect(within(console).getByText("已移除「kq-new」并删除保存的口令")).toBeInTheDocument();
});

test("概览：系统块与行情源块各读各的接口，任一失败只降级自己那一块", async () => {
  adminRuntime({ role: "owner" });
  let failServers = true;
  withAdminInfra({
    servers: () => failServers ? new Response("bad gateway", { status: 502 }) : jsonResponse(serversOk()),
    marketSources: () => failServers ? jsonResponse({ seriesPerSource: 48, items: [
      { accountId: "pool-a", position: 0, providerKind: "tqkq-pool", health: "healthy", channels: 22, channelLimit: 48, quoteSymbols: 10,
        lastError: null, lastErrorAt: null, displayName: "测试账号", accountStatus: "active" }] }) : new Response("boom", { status: 500 }),
  });
  const { unmount } = render(<AdminApp />);
  let console = await screen.findByRole("region", { name: "后台管理" });
  await waitFor(() => expect(within(console).getByText("今日 token")).toBeInTheDocument());
  expect(await within(console).findByText("服务器状态读取失败")).toBeInTheDocument();
  // 行情源照常：已用/总量入口与每源占用。
  expect(await within(console).findByRole("button", { name: "22 / 48 组 →" })).toBeInTheDocument();
  expect(within(console).getByText("测试账号 22")).toBeInTheDocument();
  unmount();
  // 反过来：行情源失败、服务器正常；每台的 CPU/内存小条在，点「服务器 →」进分区。
  failServers = false;
  render(<AdminApp />);
  console = await screen.findByRole("region", { name: "后台管理" });
  expect(await within(console).findByText("行情源读取失败")).toBeInTheDocument();
  const system = await within(console).findByRole("group", { name: "服务器状态" });
  await waitFor(() => expect(system).toHaveTextContent("主机甲"));
  expect(system).toHaveTextContent("CPU 42%");
  expect(system).toHaveTextContent("内存 85%");
  expect(within(console).getByText("今日 token")).toBeInTheDocument();
  fireEvent.click(within(system).getByRole("button", { name: "服务器 →" }));
  expect(await within(console).findByRole("list", { name: "主机" })).toBeInTheDocument();
});

/** Wraps the admin runtime's fetch: `override` answers a request first, or returns undefined to pass it on. */
function overrideFetch(override: (method: string, path: string) => Response | Promise<Response> | undefined) {
  const base = globalThis.fetch;
  vi.stubGlobal("fetch", (input: Request | string, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    return override(request.method, new URL(request.url).pathname) ?? base(input, init);
  });
}

test("概览：运维台过期的旧快照标明过期与上次时间；没有价格的成本不算成 0", async () => {
  adminRuntime({ role: "owner" });
  withAdminInfra({ servers: () => jsonResponse(serversOk({ stale: true })), marketSources: () => jsonResponse({ items: [], seriesPerSource: 48 }) });
  overrideFetch((method, path) => method === "GET" && path === "/api/admin/users" ? jsonResponse({ day: "2026-09-22", items: [
    { username: "u1", role: "user", plan: "basic", planLabel: "基础档", invitedBy: "invite-code", createdAt: "2026-09-20T08:00:00Z", lastSeenAt: null,
      tokensToday: 1_000, tokensMonth: 1_000, voiceSecondsToday: 0, voiceSecondsMonth: 0, estimatedCostToday: null, disabled: false, tqkqBound: false }] }) : undefined);
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  const system = await within(console).findByRole("group", { name: "服务器状态" });
  await waitFor(() => expect(system).toHaveTextContent("主机甲"));
  expect(system).toHaveTextContent("数据已过期（上次 16:00）");
  const tiles = within(console).getByText("今日估算成本").closest("div")!;
  expect(tiles).toHaveTextContent("—");
  expect(tiles).toHaveTextContent("另有 1 人的模型没有价格，未计入");
  expect(within(console).queryByText("0.00 元")).toBeNull();
});

test("用户分区：零匹配时筛选仍在、可一键清空；交易账户读不到时不显示成没有授权", async () => {
  adminRuntime({ role: "owner" });
  overrideFetch((method, path) => path === "/api/admin/accounts" ? new Response("boom", { status: 503 }) : undefined);
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(await within(console).findByRole("button", { name: "用户 1" }));
  const table = await within(console).findByRole("table", { name: "用户列表" });
  await waitFor(() => expect(table).toHaveTextContent("未读到"));
  expect(await within(console).findByRole("alert")).toHaveTextContent("交易账户读取失败");
  fireEvent.change(within(console).getByRole("textbox", { name: "按用户名搜索" }), { target: { value: "nobody" } });
  expect(within(console).getByText("没有匹配的用户")).toBeVisible();
  expect(within(console).getByRole("textbox", { name: "按用户名搜索" })).toBeVisible();
  fireEvent.click(within(console).getByRole("button", { name: "清空筛选" }));
  expect(await within(console).findByRole("table", { name: "用户列表" })).toHaveTextContent("u1");
  // 交易账户分区同样说读不到，而不是一直“读取中”。
  fireEvent.click(within(console).getByRole("button", { name: "交易账户" }));
  expect(await within(console).findByText("交易账户没有读到")).toBeVisible();
});

test("邀请码停用失败：行内说没有确认成功，按钮恢复可再点", async () => {
  adminRuntime({ role: "owner" });
  overrideFetch((method, path) => method === "POST" && path.endsWith("/inv-2/disable") ? new Response("busy", { status: 503 }) : undefined);
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(await within(console).findByRole("button", { name: "邀请码 2" }));
  await within(console).findByRole("table", { name: "邀请码列表" });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  const row = within(console).getByText("GATE-2026").closest("tr")!;
  fireEvent.click(within(row).getByRole("button", { name: "停用" }));
  expect(await within(row).findByRole("alert")).toHaveTextContent("停用没有确认成功，可再点一次");
  expect(within(row).getByRole("button", { name: "停用" })).toBeEnabled();
  confirm.mockRestore();
});

test("技能矩阵两格同时在途：后一格先回包后，前一格仍保持禁用直到它自己回包", async () => {
  adminRuntime({ role: "owner", registry: true });
  let landBasic: () => void = () => undefined;
  const base = globalThis.fetch;
  overrideFetch((method, path) => method === "PUT" && path === "/api/admin/plans/basic"
    ? new Promise<Response>(resolve => { landBasic = () => resolve(base(new Request(`https://hold.test${path}`, { method, body: JSON.stringify({ skills: { "ai-execution": true } }) }))); })
    : undefined);
  render(<AdminApp />);
  const console = await screen.findByRole("region", { name: "后台管理" });
  fireEvent.click(within(console).getByRole("button", { name: "技能" }));
  const matrix = await within(console).findByRole("table", { name: "技能开关矩阵" });
  fireEvent.click(within(matrix).getByRole("switch", { name: "AI 自主执行 基础档" }));
  fireEvent.click(within(matrix).getByRole("switch", { name: "AI 自主执行 高级档" }));
  await waitFor(() => expect(within(matrix).getByText("已保存")).toBeInTheDocument());
  expect(within(matrix).getByRole("switch", { name: "AI 自主执行 基础档" })).toBeDisabled();
  expect(within(matrix).getByRole("switch", { name: "AI 自主执行 高级档" })).toBeEnabled();
  await act(async () => { landBasic(); });
  await waitFor(() => expect(within(matrix).getByRole("switch", { name: "AI 自主执行 基础档" })).toBeEnabled());
});


test("管理员推送链接直接打开服务器页，仍由管理员登录门控", async () => {
  window.history.replaceState({}, "", "/trader/admin?section=servers");
  try {
    adminRuntime({ role: "owner" });
    withAdminInfra({ servers: () => jsonResponse(serversOk()) });
    render(<AdminApp />);
    const console = await screen.findByRole("region", { name: "后台管理" });
    expect(within(console).getByRole("button", { name: "服务器" })).toHaveAttribute("aria-current", "true");
    expect(await within(console).findByRole("list", { name: "主机" })).toBeInTheDocument();
  } finally {
    window.history.replaceState({}, "", "/");
  }
});
