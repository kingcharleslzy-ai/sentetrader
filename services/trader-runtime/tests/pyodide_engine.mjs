// The browser engine under Node (规则回测本机优先, 2026-10-12): the same Pyodide build the web page
// loads, the same bundle (GET /api/strategy-engine/bundle) written to the same place, the same
// `run_rule` entry. test_browser_engine_parity.py runs it and compares every result byte for byte
// with the native run. Usage: node pyodide_engine.mjs <pyodide dir> <cases.json> <out.json>
// cases.json = {bundle: {modules, entry}, cases: {name: inputs}}; out.json = {name: result json text}.
import { readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [pyodideDir, casesPath, outPath] = process.argv.slice(2);
const { loadPyodide } = await import(pathToFileURL(`${pyodideDir}/pyodide.mjs`).href);
const started = Date.now();
const pyodide = await loadPyodide({ indexURL: `${pyodideDir}/` });
await pyodide.loadPackage(["pydantic", "tzdata"], { messageCallback: () => {} });
const { bundle, cases } = JSON.parse(readFileSync(casesPath, "utf8"));
const root = "/home/pyodide";
for (const [path, source] of Object.entries(bundle.modules)) {
  const target = `${root}/${path}`;
  pyodide.FS.mkdirTree(target.slice(0, target.lastIndexOf("/")));
  pyodide.FS.writeFile(target, source, { encoding: "utf8" });
}
pyodide.runPython(`import sys; sys.path.insert(0, ${JSON.stringify(root)})`);
pyodide.runPython(`
import importlib, json as _engine_json
_engine = importlib.import_module(${JSON.stringify(bundle.entry)})
def _engine_run_rule(inputs_json):
    return _engine_json.dumps(_engine.run_rule(_engine_json.loads(inputs_json)), sort_keys=True, ensure_ascii=False)
`);
const run = pyodide.globals.get("_engine_run_rule");
const ready = Date.now() - started;
const out = {};
for (const [name, inputs] of Object.entries(cases)) out[name] = run(JSON.stringify(inputs));
writeFileSync(outPath, JSON.stringify({ readyMs: ready, runMs: Date.now() - started - ready, results: out }));
