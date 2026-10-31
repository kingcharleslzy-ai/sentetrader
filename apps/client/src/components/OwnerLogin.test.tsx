import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

const api = vi.hoisted(() => ({
  authenticateTqkq: vi.fn(),
  registerOwnerAccount: vi.fn(),
  getOwnerAuthStatus: vi.fn(),
}));
vi.mock("../api", () => api);

import { OwnerLogin } from "./OwnerLogin";

beforeEach(() => {
  window.localStorage.clear();
  api.authenticateTqkq.mockReset();
  api.registerOwnerAccount.mockReset();
  api.getOwnerAuthStatus.mockReset().mockResolvedValue({
    authenticated: false,
    paperConfigured: true,
    tqkqLogin: { enabled: false, inviteRequired: true, registerUrl: "" },
  });
});

function fill(username: string, password: string, inviteCode: string) {
  fireEvent.change(screen.getByLabelText("账号"), { target: { value: username } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("邀请码"), { target: { value: inviteCode } });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
}

test("a fresh browser's login box starts empty: no one else's name is suggested", async () => {
  vi.stubEnv("VITE_OWNER_USERNAME_HINT", "charles");
  window.localStorage.clear();
  render(<OwnerLogin baseUrl="https://runtime.test" open required onClose={vi.fn()} onAuthenticated={vi.fn()} />);
  await waitFor(() => expect(api.getOwnerAuthStatus).toHaveBeenCalled());
  expect(screen.getByLabelText("账号")).toHaveValue("");
  vi.unstubAllEnvs();
});

test("a site account logs in through the one TqKq endpoint, even with the pool off", async () => {
  api.authenticateTqkq.mockResolvedValue({
    status: "authenticated", userId: "u", username: "charles", role: "owner",
    bindings: { tqkqConfigured: false, modelCredentialConfigured: false },
  });
  const authenticated = vi.fn();
  render(<OwnerLogin baseUrl="https://runtime.test" open required onClose={vi.fn()} onAuthenticated={authenticated} />);
  await waitFor(() => expect(api.getOwnerAuthStatus).toHaveBeenCalled());

  fill("charles", "demo-password", "");

  await waitFor(() => expect(api.authenticateTqkq).toHaveBeenCalledWith("https://runtime.test", "charles", "demo-password", ""));
  expect(authenticated).toHaveBeenCalledWith(expect.stringMatching(/^已登录/));
  expect(window.localStorage.getItem("ai-trader:last-username")).toBe("charles");
});

test("a confirmed new account registers and says the site simulation account is open", async () => {
  api.authenticateTqkq.mockRejectedValue(new Error('404 {"detail":{"code":"account_not_found","canRegister":true}}'));
  api.registerOwnerAccount.mockResolvedValue({ status: "registered" });
  const authenticated = vi.fn();
  render(<OwnerLogin baseUrl="https://runtime.test" open required onClose={vi.fn()} onAuthenticated={authenticated} />);
  await waitFor(() => expect(api.getOwnerAuthStatus).toHaveBeenCalled());

  fill("new-user", "long-password-1", "invite-7");
  fireEvent.click(await screen.findByRole("button", { name: "创建并登录" }));

  await waitFor(() => expect(api.registerOwnerAccount).toHaveBeenCalledWith("https://runtime.test", "new-user", "long-password-1", "invite-7"));
  expect(authenticated).toHaveBeenCalledWith("注册成功，已开好本站模拟账户（初始资金 100 万）");
});

test("注册 opens our own sign-up form and registers through the same entry as the confirmed new account", async () => {
  api.registerOwnerAccount.mockResolvedValue({ status: "registered" });
  const authenticated = vi.fn();
  const { container } = render(<OwnerLogin baseUrl="https://runtime.test" open required onClose={vi.fn()} onAuthenticated={authenticated} />);
  await waitFor(() => expect(api.getOwnerAuthStatus).toHaveBeenCalled());
  expect(container.querySelector("a[href*='shinnytech']")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "注册" }));
  fireEvent.change(screen.getByLabelText("账号"), { target: { value: "new-user" } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "long-password-1" } });
  fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "long-password-1" } });
  fireEvent.change(screen.getByLabelText("邀请码"), { target: { value: "invite-7" } });
  fireEvent.click(screen.getByRole("button", { name: "注册并登录" }));

  await waitFor(() => expect(api.registerOwnerAccount).toHaveBeenCalledWith("https://runtime.test", "new-user", "long-password-1", "invite-7"));
  expect(api.authenticateTqkq).not.toHaveBeenCalled();
  expect(authenticated).toHaveBeenCalledWith("注册成功，已开好本站模拟账户（初始资金 100 万）");
  expect(window.localStorage.getItem("ai-trader:last-username")).toBe("new-user");
});
