import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { CloudLoginSheet } from "./CloudLoginSheet";

/** FastAPI serializes detail bodies without separator spaces; `requestJson` prefixes the status. */
function apiError(status: number, detail: unknown) {
  return new Error(`${status} ${JSON.stringify({ detail })}`);
}

function fill(username: string, password: string, inviteCode = "") {
  fireEvent.change(screen.getByLabelText("账号"), { target: { value: username } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("邀请码"), { target: { value: inviteCode } });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
}

test("the login form: 账号, 密码 and the invite for a TqKq account's first login; no link to TqKq's sign-up", () => {
  const { container } = render(<CloudLoginSheet open onClose={vi.fn()} onLogin={vi.fn()} onRegister={vi.fn()} paperConfigured />);

  expect(screen.getByRole("dialog", { name: "登录 Sente" })).toBeVisible();
  expect(screen.getByText("本站账号和快期账号都在这里登录。")).toBeVisible();
  expect(screen.queryByText(/首次登录.*即可创建|填邀请码即可创建/)).not.toBeInTheDocument();
  expect(screen.getByLabelText("账号")).toBeInTheDocument();
  expect(screen.getByLabelText("密码")).toBeInTheDocument();
  expect(screen.getByText("邀请码（快期账号首次登录填写）")).toBeVisible();
  for (const name of ["快期账号登录", "管理员登录"]) {
    expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
  }
  expect(screen.queryByText(/必须.*快期|先绑定快期|选绑/)).not.toBeInTheDocument();
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  expect(container.querySelector("a[href*='shinnytech']")).toBeNull();
  expect(screen.getByText("还没有账号？")).toBeVisible();
  expect(screen.getByRole("button", { name: "注册" })).toBeVisible();
});

test("without a register handler there is no 注册 link", () => {
  render(<CloudLoginSheet open onClose={vi.fn()} onLogin={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "注册" })).not.toBeInTheDocument();
});

function openRegister(props: Partial<Parameters<typeof CloudLoginSheet>[0]> = {}) {
  const login = vi.fn(), register = vi.fn().mockResolvedValue(undefined);
  const view = render(<CloudLoginSheet open onClose={vi.fn()} onLogin={login} onRegister={register} {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "注册" }));
  return { ...view, login, register: (props.onRegister as typeof register | undefined) ?? register };
}

function fillRegister(username: string, password: string, confirm: string, inviteCode: string) {
  fireEvent.change(screen.getByLabelText("账号"), { target: { value: username } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: confirm } });
  fireEvent.change(screen.getByLabelText("邀请码"), { target: { value: inviteCode } });
  fireEvent.click(screen.getByRole("button", { name: "注册并登录" }));
}

test("注册 switches the same sheet to the sign-up form and 已有账号？登录 switches back", () => {
  openRegister({ paperConfigured: true });
  expect(screen.getByRole("dialog", { name: "注册 Sente" })).toBeVisible();
  expect(screen.getByLabelText("密码")).toHaveAttribute("placeholder", "至少 10 位");
  expect(screen.getByLabelText("确认密码")).toBeInTheDocument();
  expect(screen.getByLabelText("邀请码")).toBeInTheDocument();
  expect(screen.getByText("新账号自带本站模拟账户（初始资金 100 万）")).toBeVisible();
  expect(screen.getByText("已有账号？")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  expect(screen.getByRole("dialog", { name: "登录 Sente" })).toBeVisible();
  expect(screen.queryByLabelText("确认密码")).not.toBeInTheDocument();
});

test("the sign-up line about the site simulation account shows only when the deployment has one", () => {
  openRegister();
  expect(screen.queryByText(/初始资金 100 万/)).not.toBeInTheDocument();
});

test("two different passwords never reach the server", () => {
  const { register } = openRegister();
  fillRegister("new-user", "long-password-1", "long-password-2", "invite-7");
  expect(screen.getByRole("alert")).toHaveTextContent("两次输入的密码不一致");
  expect(register).not.toHaveBeenCalled();
  fillRegister("new-user", "short", "short", "invite-7");
  expect(screen.getByRole("alert")).toHaveTextContent("密码至少 10 位");
  expect(register).not.toHaveBeenCalled();
});

test("a valid sign-up goes through onRegister with the trimmed invite code, never through login", async () => {
  const { register, login } = openRegister();
  fillRegister("new-user", "long-password-1", "long-password-1", " invite-7 ");
  await waitFor(() => expect(register).toHaveBeenCalledWith("new-user", "long-password-1", "invite-7"));
  expect(login).not.toHaveBeenCalled();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("a refused sign-up shows the register reason in the sign-up form", async () => {
  const refuse = vi.fn().mockRejectedValue(apiError(403, "invite_code_invalid"));
  openRegister({ onRegister: refuse });
  fillRegister("new-user", "long-password-1", "long-password-1", "used-code");
  expect(await screen.findByRole("alert")).toHaveTextContent("邀请码无效或已被使用");
  expect(screen.getByRole("dialog", { name: "注册 Sente" })).toBeVisible();
  expect(screen.getByLabelText("账号")).toHaveValue("new-user");
});

test("a returning account logs in with an empty invite code and the password is not kept", async () => {
  const login = vi.fn().mockResolvedValue(undefined);
  const register = vi.fn();
  render(<CloudLoginSheet open defaultUsername="charles" onClose={vi.fn()} onLogin={login} onRegister={register} />);

  expect(screen.getByLabelText("账号")).toHaveValue("charles");
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "demo-password" } });
  const submit = screen.getByRole("button", { name: "登录" });
  expect(submit).toBeEnabled();
  fireEvent.click(submit);

  await waitFor(() => expect(login).toHaveBeenCalledWith("charles", "demo-password", ""));
  await waitFor(() => expect(screen.getByLabelText("密码")).toHaveValue(""));
  expect(register).not.toHaveBeenCalled();
});

test.each([
  ["a TqKq check that failed", apiError(401, { code: "tqkq_login_failed", reason: "auth_failed", canRegister: true })],
  ["no TqKq pool at all", apiError(404, { code: "account_not_found", canRegister: true })],
])("an unknown new name (%s) asks before creating the account, then registers and logs in", async (_case, failure) => {
  const login = vi.fn().mockRejectedValue(failure);
  const register = vi.fn().mockResolvedValue(undefined);
  render(<CloudLoginSheet open onClose={vi.fn()} onLogin={login} onRegister={register} paperConfigured />);

  fill("new-user", "long-password-1", "invite-7");

  expect(await screen.findByRole("dialog", { name: "创建新账号？" })).toBeVisible();
  expect(screen.queryByText(/选绑|快期账号以后/)).not.toBeInTheDocument();
  expect(screen.getByText(/没有找到账号/)).toHaveTextContent("没有找到账号 new-user。用这个账号和密码创建新账号？");
  expect(screen.getByText(/初始资金 100 万/)).toBeVisible();
  expect(register).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "创建并登录" }));

  await waitFor(() => expect(register).toHaveBeenCalledWith("new-user", "long-password-1", "invite-7"));
  expect(login).toHaveBeenCalledTimes(1);
});

test("declining the new account returns to the filled form without registering", async () => {
  const register = vi.fn();
  render(
    <CloudLoginSheet open onClose={vi.fn()} onRegister={register}
      onLogin={vi.fn().mockRejectedValue(apiError(401, { code: "tqkq_login_failed", reason: "auth_failed", canRegister: true }))} />
  );
  fill("typo-name", "long-password-1", "invite-7");
  fireEvent.click(await screen.findByRole("button", { name: "返回修改" }));

  expect(screen.getByRole("dialog", { name: "登录 Sente" })).toBeVisible();
  expect(screen.getByLabelText("账号")).toHaveValue("typo-name");
  expect(screen.getByLabelText("邀请码")).toHaveValue("invite-7");
  expect(register).not.toHaveBeenCalled();
});

test("a failed registration returns to the form with the reason", async () => {
  render(
    <CloudLoginSheet open onClose={vi.fn()}
      onLogin={vi.fn().mockRejectedValue(apiError(404, { code: "account_not_found", canRegister: true }))}
      onRegister={vi.fn().mockRejectedValue(apiError(403, "invite_code_invalid"))} />
  );
  fill("new-user", "long-password-1", "used-code");
  fireEvent.click(await screen.findByRole("button", { name: "创建并登录" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("邀请码无效或已被使用");
  expect(screen.getByRole("dialog", { name: "登录 Sente" })).toBeVisible();
  expect(screen.getByLabelText("密码")).toHaveValue("");
});

test("without canRegister the form explains why instead of offering an account", async () => {
  const cases: Array<[Error, string, RegExp]> = [
    [apiError(401, { code: "tqkq_login_failed", reason: "auth_failed", canRegister: false }), "", /账号或密码不对；新账号首次登录需要填写邀请码/],
    [apiError(401, { code: "tqkq_login_failed", reason: "auth_failed", canRegister: false }), "invite-7", /^账号或密码不对$/],
    [apiError(404, { code: "account_not_found", canRegister: false }), "", /没有找到这个账号；新账号首次登录需要填写邀请码/],
    [apiError(404, { code: "account_not_found", canRegister: false }), "invite-7", /当前未开放新账号注册/],
    // A TqKq timeout is not "this account does not exist": no offer even with canRegister.
    [apiError(401, { code: "tqkq_login_failed", reason: "timeout", canRegister: true }), "invite-7", /快期连接超时/],
    [apiError(401, { code: "tqkq_login_failed", reason: "capacity_full", canRegister: false }), "", /服务器在线人数已满/],
    [apiError(401, "invalid_owner_credentials"), "", /^账号或密码不对$/],
    [apiError(403, "invite_code_invalid"), "", /新账号首次登录需要填写邀请码/],
    [apiError(403, "invite_code_invalid"), "bad", /邀请码无效或已被使用/],
    [apiError(403, { code: "use_owner_login" }), "", /这是管理员的快期账号/],
    [apiError(429, "too_many_attempts"), "", /尝试次数过多/],
  ];
  for (const [failure, invite, expected] of cases) {
    const register = vi.fn();
    const { unmount } = render(
      <CloudLoginSheet open onClose={vi.fn()} onLogin={vi.fn().mockRejectedValue(failure)} onRegister={register} />
    );
    fill("someone", "wrong-password", invite);
    expect(await screen.findByRole("alert")).toHaveTextContent(expected);
    expect(screen.queryByRole("dialog", { name: "创建新账号？" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toHaveValue("");
    expect(register).not.toHaveBeenCalled();
    unmount();
  }
});

test("a deployment that cannot register shows the not-found reason even when canRegister is set", async () => {
  render(
    <CloudLoginSheet open onClose={vi.fn()}
      onLogin={vi.fn().mockRejectedValue(apiError(404, { code: "account_not_found", canRegister: true }))} />
  );
  fill("new-user", "long-password-1", "invite-7");
  expect(await screen.findByRole("alert")).toHaveTextContent("没有找到这个账号");
});

test("required login cannot be dismissed before authentication", () => {
  const close = vi.fn();
  render(<CloudLoginSheet required open onClose={close} onLogin={vi.fn()} />);

  expect(screen.queryByRole("button", { name: "取消" })).not.toBeInTheDocument();
  fireEvent.mouseDown(screen.getByRole("presentation"));
  expect(close).not.toHaveBeenCalled();
});

test("Esc closes an optional login like 取消 and gives focus back to what opened it (#734)", () => {
  const close = vi.fn();
  const opener = document.createElement("button");
  opener.textContent = "持仓";
  document.body.append(opener);
  opener.focus();
  const behind = vi.fn();
  document.addEventListener("keydown", behind);
  const view = render(<CloudLoginSheet open onClose={close} onLogin={vi.fn()} />);
  expect(screen.getByLabelText("账号")).toHaveFocus();
  fireEvent.keyDown(screen.getByLabelText("账号"), { key: "Escape" });
  expect(close).toHaveBeenCalledOnce();
  expect(behind).not.toHaveBeenCalled();   // the workspace behind the modal never sees this Esc
  view.rerender(<CloudLoginSheet open={false} onClose={close} onLogin={vi.fn()} />);
  expect(opener).toHaveFocus();
  document.removeEventListener("keydown", behind);
  opener.remove();
});

test("Esc leaves a required login open (#734)", () => {
  const close = vi.fn();
  render(<CloudLoginSheet required open onClose={close} onLogin={vi.fn()} />);
  fireEvent.keyDown(document.body, { key: "Escape" });
  expect(close).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toBeInTheDocument();
});

test("Esc does not close the sheet while a login is pending (#734)", async () => {
  const close = vi.fn();
  render(<CloudLoginSheet open onClose={close} onLogin={() => new Promise<void>(() => undefined)} />);
  fill("13800000000", "secret");
  expect(await screen.findByRole("button", { name: "正在登录…" })).toBeDisabled();
  fireEvent.keyDown(document.body, { key: "Escape" });
  expect(close).not.toHaveBeenCalled();
});

test("a login still pending after 10 s says the server is still answering, offers no resend, and the original answer applies (#710 B)", async () => {
  vi.useFakeTimers();
  try {
    let failFirst: (error: Error) => void = () => undefined;
    const onLogin = vi.fn(() => new Promise<void>((_resolve, reject) => { failFirst = reject; }));
    render(<CloudLoginSheet open onClose={vi.fn()} onLogin={onLogin} />);
    fill("trader01", "secret");
    expect(screen.getByRole("button", { name: "正在登录…" })).toBeDisabled();
    act(() => { vi.advanceTimersByTime(9_999); });
    expect(screen.queryByText("还在等服务器响应，请稍候")).not.toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1); });
    expect(screen.getByRole("status")).toHaveTextContent("还在等服务器响应，请稍候");
    expect(screen.queryByRole("button", { name: /重试|重新登录/ })).not.toBeInTheDocument();
    fireEvent.submit(screen.getByRole("button", { name: "正在登录…" }).closest("form")!);
    expect(onLogin).toHaveBeenCalledOnce();
    await act(async () => { failFirst(apiError(401, "invalid_owner_credentials")); });
    expect(screen.getByRole("alert")).toHaveTextContent("账号或密码不对");
    expect(screen.queryByText("还在等服务器响应，请稍候")).not.toBeInTheDocument();
    expect(onLogin).toHaveBeenCalledOnce();
  } finally {
    vi.useRealTimers();
  }
});

test("a registration still pending says so the same way and never sends a second POST (#710 B)", () => {
  vi.useFakeTimers();
  try {
    const onRegister = vi.fn(() => new Promise<void>(() => undefined));
    render(<CloudLoginSheet open onClose={vi.fn()} onLogin={vi.fn()} onRegister={onRegister} />);
    fireEvent.click(screen.getByRole("button", { name: "注册" }));
    fireEvent.change(screen.getByLabelText("账号"), { target: { value: "new_user" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "long-enough-1" } });
    fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "long-enough-1" } });
    fireEvent.change(screen.getByLabelText("邀请码"), { target: { value: "INVITE" } });
    fireEvent.click(screen.getByRole("button", { name: "注册并登录" }));
    act(() => { vi.advanceTimersByTime(10_000); });
    expect(screen.getByRole("status")).toHaveTextContent("还在等服务器响应，请稍候");
    expect(screen.queryByRole("button", { name: /重试/ })).not.toBeInTheDocument();
    expect(onRegister).toHaveBeenCalledOnce();
  } finally {
    vi.useRealTimers();
  }
});
