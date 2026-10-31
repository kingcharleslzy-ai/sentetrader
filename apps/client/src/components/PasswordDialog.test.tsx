import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { useState } from "react";
import { PasswordDialog } from "./PasswordDialog";

function Harness({ onSubmit }: { onSubmit: (current: string, next: string) => Promise<void> }) {
  const [open, setOpen] = useState(true);
  return <>
    <span>{open ? "open" : "closed"}</span>
    <PasswordDialog surface="change-password" open={open} onOpenChange={setOpen} title="修改登录密码" subject="charles"
      needCurrent note="改好后这台设备保持登录，其他设备需要用新密码重新登录。" submitLabel="修改密码" onSubmit={onSubmit} />
  </>;
}

const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

test("修改密码: 两次一致且够 10 位才能提交，当前密码不对时原样提示", async () => {
  const onSubmit = vi.fn()
    .mockRejectedValueOnce(Object.assign(new Error("401"), { status: 401, detail: { detail: "invalid_current_password" } }))
    .mockResolvedValueOnce(undefined);
  render(<Harness onSubmit={onSubmit} />);
  const submit = screen.getByRole("button", { name: "修改密码" });
  type("当前密码", "old-password-1");
  type("新密码", "short");
  expect(screen.getByText("新密码至少 10 位")).toBeTruthy();
  type("新密码", "brand-new-password");
  type("再输一次新密码", "brand-new-passwor");
  expect(screen.getByText("两次输入的新密码不一样")).toBeTruthy();
  expect((submit as HTMLButtonElement).disabled).toBe(true);
  type("再输一次新密码", "brand-new-password");
  expect((submit as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(submit);
  expect((await screen.findByRole("alert")).textContent).toBe("当前密码不对");
  expect(screen.getByText("open")).toBeTruthy();
  fireEvent.click(submit);
  await waitFor(() => expect(screen.getByText("closed")).toBeTruthy());
  expect(onSubmit).toHaveBeenLastCalledWith("old-password-1", "brand-new-password");
});
