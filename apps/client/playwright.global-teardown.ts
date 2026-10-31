import { chmodSync, existsSync, lstatSync, readdirSync, rmSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { tmpdir } from "node:os";

function makeWritable(path: string) {
  const stat = lstatSync(path);
  if (stat.isSymbolicLink()) return;
  if (!stat.isDirectory()) {
    chmodSync(path, 0o600);
    return;
  }
  chmodSync(path, 0o700);
  for (const entry of readdirSync(path)) makeWritable(resolve(path, entry));
}

export default function globalTeardown() {
  const stateRoot = process.env.AI_TRADER_E2E_STATE_ROOT;
  if (!stateRoot) return;
  const resolved = resolve(stateRoot);
  const testStateParent = resolve(process.env.RUNNER_TEMP || tmpdir());
  if (
    dirname(resolved) !== testStateParent
    || !basename(resolved).startsWith("ai-trader-playwright-")
  ) {
    throw new Error("refusing_to_remove_non_test_state_root");
  }
  if (!existsSync(resolved)) return;
  makeWritable(resolved);
  rmSync(resolved, { recursive: true, force: true });
}
