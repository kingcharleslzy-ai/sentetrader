import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { TradingAccountDirectory } from "../../api";
import { NO_LIVE_ACCOUNT, TradingAccountSelect, simulationAccountText, switchableAccounts, tradingAccountLabel } from "./TradingAccountSelect";

const directory = (accounts: TradingAccountDirectory["accounts"]): TradingAccountDirectory =>
  ({ accounts, current: accounts[0]?.id ?? "" });

const twoAccounts = directory([
  { id: "tqkq-sim:default", displayName: "默认模拟账户", role: "owner", environment: "simulation", executionOwner: true },
  { id: "tqkq-sim:abc123def456", displayName: "第二模拟账户", role: "operator", environment: "simulation", executionOwner: false },
]);

test("a single granted account renders no switcher", () => {
  const { container } = render(<TradingAccountSelect
    directory={directory([{ id: "tqkq-sim:default", displayName: "默认模拟账户", role: "owner", environment: "simulation", executionOwner: true }])}
    currentId="tqkq-sim:default" onSwitch={vi.fn()} />);
  expect(container).toBeEmptyDOMElement();
});

test("two accounts render the select with the current account selected; only others' accounts carry a role", () => {
  render(<TradingAccountSelect directory={twoAccounts} currentId="tqkq-sim:default" onSwitch={vi.fn()} />);
  const select = screen.getByTestId("trading-account-select").querySelector("select");
  expect(select).toHaveValue("tqkq-sim:default");
  expect(screen.getByRole("option", { name: "默认模拟账户" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "第二模拟账户（操作员）" })).toBeInTheDocument();
});

test("accounts are grouped 模拟盘 / 实盘; 实盘 has only the unselectable 暂无 entry", () => {
  render(<TradingAccountSelect directory={twoAccounts} currentId="tqkq-sim:default" onSwitch={vi.fn()} />);
  const simulation = screen.getByRole("group", { name: "模拟盘" });
  expect(within(simulation).getAllByRole("option").map(option => option.textContent)).toEqual(["默认模拟账户", "第二模拟账户（操作员）"]);
  const live = screen.getByRole("group", { name: "实盘" });
  const none = within(live).getByRole("option", { name: NO_LIVE_ACCOUNT });
  expect(none).toBeDisabled();
});

test("labels say what the account is: 本站模拟, else the display name", () => {
  expect(tradingAccountLabel({ displayName: "快期模拟账户", providerKind: "tqkq-sidecar", role: "owner" })).toBe("快期模拟账户");
  expect(tradingAccountLabel({ displayName: "本站模拟账户", providerKind: "paper", role: "owner" })).toBe("本站模拟");
  // The 外盘 sub-account is its own entry: it no longer reads the same as the RMB one (owner 2026-10-21).
  expect(tradingAccountLabel({ displayName: "本站模拟 · 美元", providerKind: "paper", role: "owner", currency: "USD" })).toBe("本站模拟 · 美元");
  expect(tradingAccountLabel({ displayName: "团队账户", providerKind: "tqkq-sidecar", role: "viewer" })).toBe("团队账户（只读）");
  expect(tradingAccountLabel({ displayName: "默认模拟账户", role: "operator" })).toBe("默认模拟账户（操作员）");
  expect(simulationAccountText({ displayName: "本站模拟账户", providerKind: "paper", role: "owner" })).toBe("模拟盘 · 本站模拟");
});

test("choosing another account switches through the session route and stays disabled until it settles", async () => {
  let release: (() => void) | undefined;
  const onSwitch = vi.fn(() => new Promise<string | undefined>(resolve => { release = () => resolve(undefined); }));
  render(<TradingAccountSelect directory={twoAccounts} currentId="tqkq-sim:default" onSwitch={onSwitch} />);
  fireEvent.change(screen.getByRole("combobox", { name: "切换交易账户" }), { target: { value: "tqkq-sim:abc123def456" } });
  expect(onSwitch).toHaveBeenCalledWith("tqkq-sim:abc123def456");
  expect(screen.getByRole("combobox", { name: "切换交易账户" })).toBeDisabled();
  release?.();
  await waitFor(() => expect(screen.getByRole("combobox", { name: "切换交易账户" })).toBeEnabled());
  // The runtime.status that follows the switch drives the selection; until then it stays on the current account.
  expect(screen.getByRole("combobox", { name: "切换交易账户" })).toHaveValue("tqkq-sim:default");
});

test("a failed switch shows the Chinese message beside the select and recovers", async () => {
  const onSwitch = vi.fn(async () => "切换交易账户未完成，请重试。");
  render(<TradingAccountSelect directory={twoAccounts} currentId="tqkq-sim:default" onSwitch={onSwitch} />);
  fireEvent.change(screen.getByRole("combobox", { name: "切换交易账户" }), { target: { value: "tqkq-sim:abc123def456" } });
  const status = await screen.findByRole("status");
  expect(status).toHaveTextContent("切换交易账户未完成，请重试。");
  expect(screen.getByRole("combobox", { name: "切换交易账户" })).toBeEnabled();
  expect(screen.getByRole("combobox", { name: "切换交易账户" })).toHaveValue("tqkq-sim:default");
});

test("本站模拟合成一个账户: the 外盘 USD ledger is not offered on its own unless the session sits on it (2026-10-21)", () => {
  const accounts = directory([
    { id: "tqkq-sim:default", displayName: "快期模拟账户", role: "owner", environment: "simulation", executionOwner: true, providerKind: "tqkq-sidecar" },
    { id: "paper:u1", displayName: "本站模拟", role: "owner", environment: "simulation", executionOwner: true, providerKind: "paper", currency: "CNY" },
    { id: "paperusd:u1", displayName: "本站模拟 · 美元", role: "owner", environment: "simulation", executionOwner: true, providerKind: "paper", currency: "USD" },
  ]);
  expect(switchableAccounts(accounts, "paper:u1").map(entry => entry.id)).toEqual(["tqkq-sim:default", "paper:u1"]);
  expect(switchableAccounts(accounts, "paperusd:u1").map(entry => entry.id)).toEqual(["tqkq-sim:default", "paper:u1", "paperusd:u1"]);
  render(<TradingAccountSelect directory={accounts} currentId="paper:u1" onSwitch={vi.fn()} />);
  expect(within(screen.getByRole("group", { name: "模拟盘" })).getAllByRole("option").map(option => option.textContent))
    .toEqual(["快期模拟账户", "本站模拟"]);
});
