import { randomBytes } from "node:crypto";
import { spawn } from "node:child_process";

if (process.env.AI_TRADER_DATA_MODE === "tqkq-readonly") {
  console.error("TqKq 只读模式禁止通过 npm run dev / uvicorn --reload 启动；真实凭据必须等待 Phase 1.4 隔离 sidecar。当前可继续使用默认假数据模式。");
  process.exit(2);
}

const token = randomBytes(32).toString("base64url");
const environment = {
  ...process.env,
  AI_TRADER_LOCAL_TOKEN: token,
  VITE_LOCAL_RUNTIME_TOKEN: token,
  VITE_CLOUD_RUNTIME_TOKEN: token
};

const children = ["dev:runtime", "dev:client"].map((script) =>
  spawn("npm", ["run", script], { env: environment, stdio: "inherit" })
);

const stop = () => children.forEach((child) => child.kill("SIGTERM"));
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
const results = await Promise.all(children.map((child) => new Promise((resolve) => child.on("exit", resolve))));
process.exit(results.some((code) => code !== 0) ? 1 : 0);
