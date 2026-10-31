import { spawnSync } from "node:child_process";
import { existsSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import openapiTS, { astToString } from "openapi-typescript";

const root = fileURLToPath(new URL("../", import.meta.url));
const localPython = resolve(root, "services/trader-runtime/.venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const python = process.env.AI_TRADER_SCHEMA_PYTHON || (existsSync(localPython) ? localPython : "python3");
const result = spawnSync(python, ["-B", resolve(root, "scripts/export_workbench_openapi.py")], { cwd: root, encoding: "utf8" });
if (result.error) throw result.error;
if (result.status !== 0) throw new Error(result.stderr || "OpenAPI export failed");
const source = astToString(await openapiTS(JSON.parse(result.stdout)));
writeFileSync(resolve(root, "apps/client/src/workbench.generated.ts"),
  "// Generated from Runtime routes by npm run generate:workbench-api. Do not edit.\n" + source);
