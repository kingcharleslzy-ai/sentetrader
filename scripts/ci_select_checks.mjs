// Change-aware test selection (2026-10-05; Python test targets 2026-10-12). One place, used by the
// CI `changes` job and the local entry `npm run test:changed`
// (`node scripts/ci_select_checks.mjs -- <path>...` prints the plan for given paths):
// the changed paths of a PR decide which test areas run and, for the Python areas, which test
// files. Fail-closed by design: any path no area claims selects the full suite, and a change the
// Python plan cannot place runs that whole area — over-running beats under-running.
//
// Area → CI work (see .github/workflows/ci.yml for the step-level `if`):
//   client   → verify-linux (lint:web, build web, test:unit)
//   runtime  → verify-mac test:runtime (the affected test files on a PR), verify-linux lint:python
//   sidecar  → verify-mac test:sidecar (the affected test files on a PR), verify-linux lint:python
//   workspace→ verify-mac test:workspace (two tests, always whole) + the whole runtime suite
//   full     → everything, web AND python halves, every suite whole (CI/workflow/deploy/desktop/
//              root package.json — it carries every test/build script —, unclaimed paths)
//
// Python test files of an area (planChecks → python[area].tests): the changed test files, plus every test file whose
// imports reach a changed module (transitive import graph over both services' src/ and the area's
// tests/; running a module as a subprocess — `"-m", "pkg.module"` or a `"name.py"` path literal —
// counts as importing it), plus every test file reaching a module that names a changed non-Python file by path
// (a string literal such as `REPO_ROOT / "skills" / "ai-trader" / "SKILL.md"`). The literal scan
// replaces hand-kept input lists: a text file outside services/ (skills/**, docs/**, packages/**
// fixtures) selects exactly the tests that read it, or none. Package data under src/
// (help/catalog.json) selects the tests naming its full path. Whole area instead (null): __init__,
// conftest, pyproject/lock/Dockerfile, package data no test names by full path (agent_skills/*.md,
// strategy templates), a deleted module or test (callers list paths with `git diff --no-renames`, so
// a rename arrives as deletion plus addition), a file inside the service that no module names,
// and workspace/ (the default engineering root is the
// repository, so tests read workspace/ without naming it). Nested tests/ helpers (fixtures/*.py) are
// graph nodes named by dotted path (`from fixtures.x import ...`); one no test imports, and any changed
// module while a source launches a subprocess with a target the scan cannot resolve (`-m` + a
// variable, a `.py` name built at runtime), also run the whole area.

import { appendFileSync, existsSync, readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));

// Source files another area's tests read across the service boundary: editing one also selects
// that area. Client tests import two Runtime modules with `?raw` and the client imports the interface surface
// table (ui_surfaces.json); the sidecar's TqSdk parity test
// imports the Runtime indicators; Runtime tests import sidecar constants and run the paper engine
// (with the sidecar's test_paper_book helpers) in a subprocess.
const CROSS_AREA_INPUTS = new Map([
  ["services/trader-runtime/src/trader_runtime/strategy_rules.py", "client"],
  ["services/trader-runtime/src/trader_runtime/simulation_orders.py", "client"],
  ["services/trader-runtime/src/trader_runtime/ui_surfaces.json", "client"],
  ["services/trader-runtime/src/trader_runtime/indicators.py", "sidecar"],
  ["services/tqkq-sidecar/src/tqkq_readonly_sidecar/main.py", "runtime"],
  ["services/tqkq-sidecar/src/tqkq_readonly_sidecar/paper.py", "runtime"],
  ["services/tqkq-sidecar/src/tqkq_readonly_sidecar/paper_book.py", "runtime"],
  ["services/tqkq-sidecar/tests/test_paper_book.py", "runtime"],
]);

// CI/workflow/packaging/shared build surface: a change here can move any job, so
// the whole suite runs. The root package.json carries every test/build script, so
// it is full too; package-lock.json stays client.
const FULL_AREAS = [".github/", "scripts/", "deploy/", "apps/desktop/"];
const FULL_FILES = new Set(["package.json", "docker-compose.cloud.yml"]);

// Text read by tests, not code: claimed (never unclaimed → full) but no area of its own — the
// Python plan's literal scan selects the tests that name the file, the client's vitest graph
// covers `?raw` imports. skills/ai-trader/SKILL.md is the external agent guide.
const TEXT_AREAS = ["skills/"];

function isDocs(path) {
  return path.endsWith(".md") || path.startsWith("docs/");
}

export function selectChecks(paths) {
  const areas = { client: false, runtime: false, sidecar: false, workspace: false, full: false };
  const reasons = [];
  for (const path of paths) {
    let reason;
    if (FULL_AREAS.some((prefix) => path.startsWith(prefix)) || FULL_FILES.has(path)) {
      areas.full = true;
      reason = "ci/build surface";
    } else if (path.startsWith("services/trader-runtime/")) {
      areas.runtime = true;
      reason = "runtime";
    } else if (path.startsWith("services/tqkq-sidecar/")) {
      areas.sidecar = true;
      reason = "sidecar";
    } else if (path.startsWith("workspace/")) {
      // Runtime tests read workspace/ through the default engineering root, and it has its own suite.
      areas.runtime = true;
      areas.workspace = true;
      reason = "workspace";
    } else if (path.startsWith("apps/client/") || path.startsWith("packages/")
      || path === "package-lock.json") {
      areas.client = true;
      reason = "client";
    } else if (TEXT_AREAS.some((prefix) => path.startsWith(prefix))) {
      reason = "text input";
    } else if (isDocs(path)) {
      reason = "docs";
    } else {
      // Unclaimed path: run everything rather than guess.
      areas.full = true;
      reason = "unclaimed → full";
    }
    const crossArea = CROSS_AREA_INPUTS.get(path);
    if (crossArea) {
      areas[crossArea] = true;
      reason += ` + ${crossArea} test input`;
    }
    reasons.push(`${path}: ${reason}`);
  }
  const docsOnly = paths.length > 0 && Object.values(areas).every((selected) => !selected);
  return { ...areas, docsOnly, reasons };
}

export const PYTHON = {
  runtime: { project: "services/trader-runtime", prefix: "services/trader-runtime/",
    tests: "services/trader-runtime/tests", src: "services/trader-runtime/src/" },
  sidecar: { project: "services/tqkq-sidecar", prefix: "services/tqkq-sidecar/",
    tests: "services/tqkq-sidecar/tests", src: "services/tqkq-sidecar/src/" },
};

// Python imports of one file, as dotted module names (relative ones resolved against `self`),
// including import lines of a script held in a string (a test's subprocess).
function pythonImports(text, self, isPackage) {
  const found = new Set();
  const here = isPackage ? self.split(".") : self.split(".").slice(0, -1);
  const names = (list) => list.replace(/[()\\]/g, " ").split(",")
    .map((item) => item.trim().split(/\s+/)[0]).filter((name) => /^\w+$/.test(name));
  for (const match of text.matchAll(/^[ \t]*["']?from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)/gm)) {
    const [, dots, path, list] = match;
    const parts = dots ? [...here.slice(0, here.length - (dots.length - 1)), ...(path ? path.split(".") : [])] : path.split(".");
    const module = parts.join(".");
    found.add(module);
    for (const name of names(list)) found.add(`${module}.${name}`);
  }
  for (const match of text.matchAll(/^[ \t]*["']?import[ \t]+([\w., \t]+)/gm)) {
    for (const name of match[1].split(",")) found.add(name.trim().split(/\s+/)[0]);
  }
  // A module run as a subprocess entry (`sys.executable, "-m", "pkg.module"` or `python -m pkg.module`):
  // the launcher depends on it although nothing imports it.
  for (const match of text.matchAll(/(?:["']-m["']\s*,\s*["']|-m[ \t]+)([A-Za-z_]\w*(?:\.\w+)+)/g)) {
    found.add(match[1]);
  }
  return found;
}

// Python files a source names by a `.py` path literal — a script it runs by path
// (`Path(__file__).with_name("backtest_worker.py")`, `ROOT / "src/pkg/main.py"`): every known
// module file the literal is a path suffix of. `byName` maps a file basename to its modules.
function pythonFileLiterals(text, byName, files) {
  const found = new Set();
  for (const match of text.matchAll(/["']([\w./-]*?)([\w-]+\.py)["']/g)) {
    const [, dir, name] = match;
    for (const module of byName.get(name) ?? []) {
      if (!dir || files.get(module).endsWith(`/${dir}${name}`)) found.add(module);
    }
  }
  return found;
}

// A source that launches a subprocess whose target the literal scan cannot resolve: `"-m"` followed by a
// variable, f-string or concatenation, a `-m {module}` command template, or a `.py` name built at
// runtime (f"{name}.py", "name" + ".py", NAME + ".py" — #932 X2, "%s.py", with_suffix(".py")) next to a process launch. Such an
// edge is invisible to the import graph, so a changed module selects the whole area instead of a
// partial list that may miss the launched code.
const LAUNCHES = /\bsys\.executable\b|\bsubprocess\b|\bPopen\b|\brunpy\b|create_subprocess/;
function hasUnresolvedLaunch(text) {
  for (const [, next] of text.matchAll(/["']-m["']\s*,\s*([^\n]{0,80})/g)) {
    if (!/^["'][\w.]+["']\s*[,)\]]/.test(next)) return true;
  }
  if (/-m[ \t]+[{$%]/.test(text)) return true;
  if (!LAUNCHES.test(text)) return false;
  return /\{[^}]*\}[\w.-]*\.py["']|[\w\])]\s*\+\s*["'][\w-]*\.py["']|["']\s*\+\s*["'][^"']*\.py["']|["']\s*\+\s*[\w.]+\s*\+\s*["']\.py|%s[\w-]*\.py|with_suffix\(\s*["']\.py/.test(text);
}

const escape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// Segment joints inside one literal ("a/b") or between literals ("a" / "b", "a", "b").
const JOINT = `(?:/|["']\\s*[/,]\\s*["'])`;

// Does Python source name `path` (a/b/c.ext) by string literals? The file itself in any split, or
// a directory holding it (copied or walked whole) — a multi-segment directory as a whole literal
// chain, a single top-level directory only when joined onto a path (`ROOT / "workspace"`).
const patternCache = new Map();
function pathPatterns(path) {
  if (patternCache.has(path)) return patternCache.get(path);
  const segments = path.split("/");
  const chain = (parts) => `["']${parts.map(escape).join(JOINT)}["']`;
  const end = `(?!\\s*[/,]\\s*["'])`;
  const patterns = [new RegExp(chain(segments))];
  for (let length = segments.length - 1; length >= 1; length -= 1) {
    const head = chain(segments.slice(0, length));
    patterns.push(new RegExp(length === 1 ? `/\\s*${head}${end}` : `${head}${end}`));
  }
  patternCache.set(path, patterns);
  return patterns;
}
const namesPath = (text, path) => pathPatterns(path).some((pattern) => pattern.test(text));

// Files inside a service that are code or configuration no literal names: the whole area runs.
function wholeAreaFile(relative) {
  return /^(pyproject\.toml|uv\.lock|\.python-version|Dockerfile[^/]*)$/.test(relative);
}

// Package data (a non-.py file under a service's src/) is loaded by modules nearly every test imports,
// so only a test naming its full path — repository-, service- or package-relative, never a directory —
// counts as reading it (help/catalog.json). Returns the naming test modules of `testsDir`.
function testsNamingFully(root, testsDir, path) {
  const service = Object.values(PYTHON).find((item) => path.startsWith(item.src));
  const relative = path.slice(service.prefix.length);
  const patterns = [path, relative, relative.slice("src/".length)].map((form) => pathPatterns(form)[0]);
  return readdirSync(join(root, testsDir)).filter((name) => name.endsWith(".py"))
    .filter((name) => patterns.some((pattern) => pattern.test(readFileSync(join(root, testsDir, name), "utf8"))))
    .map((name) => name.slice(0, -3));
}

// The test files of one Python area a change reaches. null = run the whole area, [] = none.
function areaTargets(area, paths, root, selection) {
  const { tests, src, prefix } = PYTHON[area];
  const other = PYTHON[area === "runtime" ? "sidecar" : "runtime"];
  const notes = [];
  const whole = (why) => ({ tests: null, notes: [...notes, why] });
  if (selection.full) return whole("full");
  if (area === "runtime" && selection.workspace) return whole("workspace/ (默认工程根读取，测试不按路径命名)");
  // An other-service test file this area runs as a helper has no import edge here.
  if (paths.some((path) => path.startsWith(`${other.tests}/`) && CROSS_AREA_INPUTS.get(path) === area)) {
    return whole("另一服务的测试辅助文件");
  }
  // Module name → file, for src/ (dotted) and tests/ (bare stem for a top-level file, dotted package
  // path for a nested helper such as fixtures.fake_responses_server — pytest puts tests/ on sys.path).
  const files = new Map();
  const walk = (dir, parts) => {
    for (const entry of readdirSync(join(root, dir), { withFileTypes: true })) {
      if (entry.isDirectory()) walk(`${dir}/${entry.name}`, [...parts, entry.name]);
      else if (entry.name.endsWith(".py")) {
        const stem = entry.name.slice(0, -3);
        files.set(stem === "__init__" ? parts.join(".") : [...parts, stem].join("."), `${dir}/${entry.name}`);
      }
    }
  };
  walk(src.slice(0, -1), []);
  walk(other.src.slice(0, -1), []);
  walk(tests, []);
  const sources = new Map([...files].map(([module, file]) => [module, readFileSync(join(root, file), "utf8")]));
  const byName = new Map();
  for (const [module, file] of files) {
    const name = file.slice(file.lastIndexOf("/") + 1);
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name).push(module);
  }
  // Reverse edges: module → modules that import it, run it with `-m` or run it by file path.
  const importers = new Map();
  for (const [module, file] of files) {
    const text = sources.get(module);
    for (const imported of [...pythonImports(text, module, file.endsWith("__init__.py")),
      ...pythonFileLiterals(text, byName, files)]) {
      if (!files.has(imported) || imported === module) continue;
      if (!importers.has(imported)) importers.set(imported, new Set());
      importers.get(imported).add(module);
    }
  }
  const reached = new Set();
  const visit = (module) => {
    if (reached.has(module)) return;
    reached.add(module);
    for (const importer of importers.get(module) ?? []) visit(importer);
  };
  const isTestModule = (name) => /^test_\w+\.py$/.test(files.get(name)?.slice(files.get(name).lastIndexOf("/") + 1) ?? "")
    && files.get(name).startsWith(`${tests}/`);
  // Sources whose subprocess launch target the import graph cannot resolve.
  const unresolved = [...sources].filter(([, text]) => hasUnresolvedLaunch(text)).map(([module]) => module);
  for (const path of paths) {
    const inArea = path.startsWith(prefix);
    const crossSource = path.startsWith(other.src) && path.endsWith(".py");
    if ((path.startsWith(src) || path.startsWith(other.src)) && !path.endsWith(".py")) {
      // Package data: exactly the tests naming its full path; none of this area's tests while the
      // other area's do → not this area's input; no test anywhere names it → whole area.
      const naming = testsNamingFully(root, tests, path);
      if (naming.length) {
        notes.push(`${path} ← ${naming.join(", ")}（完整路径）`);
        naming.forEach(visit);
        continue;
      }
      if (testsNamingFully(root, other.tests, path).length) continue;
      return whole(`包数据 ${path} 无测试按完整路径引用`);
    }
    if (!inArea && !crossSource) {
      // Text outside this service: the modules that name it by path.
      if (path.startsWith(other.prefix)) continue;
      const naming = [...sources].filter(([, text]) => namesPath(text, path)).map(([module]) => module);
      if (naming.length) notes.push(`${path} ← ${naming.join(", ")}`);
      naming.forEach(visit);
      continue;
    }
    // A module under tests/ at any depth: a test file, conftest or a helper the tests import
    // (fixtures/fake_responses_server.py is imported as fixtures.fake_responses_server).
    const inTests = path.startsWith(`${tests}/`) && path.endsWith(".py");
    if (inTests && path.endsWith("/conftest.py")) return whole("conftest 改动（每个测试都跑它的夹具）");
    if (inTests && !existsSync(join(root, path))) return whole(`删除的测试模块 ${path}`);
    const home = crossSource ? other.src : src;
    if (inTests || (path.startsWith(home) && path.endsWith(".py"))) {
      if (path.endsWith("__init__.py")) return whole(`包入口 ${path}`);
      const module = (inTests ? path.slice(tests.length + 1, -3) : path.slice(home.length, -3)).split("/").join(".");
      if (!files.has(module)) return whole(`删除的模块 ${path}`);
      if (unresolved.length && !(inTests && isTestModule(module))) {
        return whole(`无法解析子进程启动目标（${unresolved.join(", ")}），${path} 可能是被启动的代码`);
      }
      const before = reached.size;
      visit(module);
      // A nested tests/ helper no test reaches by import is loaded some way the graph cannot see.
      if (inTests && module.includes(".") && ![...reached].slice(before).some(isTestModule)) {
        return whole(`嵌套测试辅助 ${path} 无导入路径`);
      }
      // A cross-area input no test of this area imports is read some other way (the paper engine
      // subprocess): run the whole area.
      if (crossSource && CROSS_AREA_INPUTS.get(path) === area
        && ![...reached].slice(before).some(isTestModule)) return whole(`跨区输入 ${path} 无导入路径`);
      continue;
    }
    // Any other file inside this service (fixtures, contracts, eval scripts, README): the modules
    // naming it by service-, tests- or repository-relative path; code/config/package data → whole.
    const relative = path.slice(prefix.length);
    if (wholeAreaFile(relative)) return whole(`服务配置 ${path}`);
    const forms = [path, relative];
    if (path.startsWith(`${tests}/`)) forms.push(path.slice(tests.length + 1));
    // A script or helper (.py) no module models is matched by its own path or name only: a directory
    // some unrelated test happens to name must not stand in for its consumers.
    const matches = path.endsWith(".py")
      ? (text) => [...forms, path.slice(path.lastIndexOf("/") + 1)].some((form) => pathPatterns(form)[0].test(text))
      : (text) => forms.some((form) => namesPath(text, form));
    const naming = [...sources].filter(([, text]) => matches(text)).map(([module]) => module);
    if (naming.length === 0 && !path.endsWith(".md")) return whole(`无模块按路径引用 ${path}`);
    if (naming.length) notes.push(`${path} ← ${naming.join(", ")}`);
    naming.forEach(visit);
  }
  if (reached.has("conftest")) return whole("conftest 可达（每个测试都跑它的夹具）");
  const targets = [...reached].filter(isTestModule).map((module) => files.get(module)).sort();
  return { tests: targets, notes };
}

/**
 * The whole plan for a set of changed paths: the areas (selectChecks, plus any Python area whose
 * modules name a changed text file) and, per Python area, `tests`: null = whole area, [] = no test,
 * else the affected test files.
 */
export function planChecks(paths, { root = repoRoot } = {}) {
  const selection = selectChecks(paths);
  const python = {};
  for (const area of ["runtime", "sidecar"]) {
    const plan = areaTargets(area, paths, root, selection);
    if (selection[area] || selection.full || plan.tests === null || plan.tests.length > 0) {
      selection[area] = true;
      python[area] = plan;
    }
  }
  selection.docsOnly = selection.docsOnly && Object.keys(python).length === 0;
  return { ...selection, python };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const githubOutput = args[0] === "--github-output";
  const rest = githubOutput ? args.slice(1) : args;
  const paths = rest[0] === "--" ? rest.slice(1) : rest;
  if (paths.length === 0) {
    console.error("usage: node scripts/ci_select_checks.mjs [--github-output] -- <changed-path>...");
    process.exit(2);
  }
  const plan = planChecks(paths);
  // Coarse job areas the workflow gates on: the web half (verify-linux) and the python half
  // (lint:python on verify-linux, the suites on verify-mac).
  // `full` turns every area on — a "full" run that skipped the backend suites would not be full.
  const web = plan.client || plan.full;
  const python = plan.runtime || plan.sidecar || plan.workspace || plan.full;
  // Per Python area: "all" (whole suite), "" (nothing to run) or the space-separated test files.
  const testsOutput = (area) => {
    const tests = plan.python[area]?.tests;
    if (tests === undefined) return "";
    return tests === null ? "all" : tests.join(" ");
  };
  const lines = [
    `web=${web}`, `python=${python}`,
    `runtime=${plan.runtime || plan.full}`,
    `sidecar=${plan.sidecar || plan.full}`,
    `workspace=${plan.workspace || plan.full}`,
    `full=${plan.full}`,
    `runtime_tests=${testsOutput("runtime")}`,
    `sidecar_tests=${testsOutput("sidecar")}`,
  ];
  const explain = [
    ...plan.reasons.map((line) => `  ${line}`),
    ...Object.entries(plan.python).map(([area, { tests, notes }]) =>
      `  ${area}: ${tests === null ? "整区" : `${tests.length} 个测试文件`}${notes.length ? `（${notes.join("；")}）` : ""}`),
  ].join("\n");
  if (githubOutput) {
    const target = process.env.GITHUB_OUTPUT;
    if (!target) {
      console.error("GITHUB_OUTPUT is not set");
      process.exit(2);
    }
    appendFileSync(target, lines.map((line) => `${line}\n`).join(""));
    console.log(lines.join("\n"));
    console.log(explain);
  } else {
    console.log(lines.join("\n"));
    console.log(explain);
  }
}
