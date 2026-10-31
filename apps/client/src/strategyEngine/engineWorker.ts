/**
 * 回测本机优先 (2026-10-12): the browser's backtest worker. It loads Pyodide (a module worker: Pyodide
 * refuses classic workers), writes the Runtime's engine bundle (`GET /api/strategy-engine/bundle`, the
 * server's own sources keyed by digest) into /home/pyodide and runs, one message at a time, either a rule
 * backtest (`run_rule` over the `inputs` of `POST /api/strategy-backtests/inputs`) or one 比一比 variant
 * (`run_variant` over the lab's inputs). Only JSON strings cross the JS/Python boundary, as the Runtime
 * wrote them (a JavaScript round trip would turn 3500.0 into 3500). The same entry functions run in
 * the server's worker processes; services/trader-runtime/tests/test_browser_engine_parity.py pins that
 * this Pyodide build gives the native results byte for byte.
 */
type InitMessage = { type: "init"; id: number; pyodideUrl: string; bundle: { modules: Record<string, string>; entry: string } };
type RuleMessage = { type: "rule"; id: number; inputsJson: string };
type VariantMessage = { type: "variant"; id: number; key: string; inputsJson: string; variant: unknown };
type InMessage = InitMessage | RuleMessage | VariantMessage;

const BRIDGE = (entry: string) => `
import importlib, json as _engine_json
_engine = importlib.import_module(${JSON.stringify(entry)})
def _engine_run_rule(payload_json):
    return _engine_json.dumps(_engine.run_rule(_engine_json.loads(payload_json)["inputs"]), ensure_ascii=False)
_engine_inputs = {}
def _engine_run_variant(key, inputs_json, variant_json):
    if key not in _engine_inputs:
        _engine_inputs.clear()
        _engine_inputs[key] = _engine_json.loads(inputs_json)
    return _engine_json.dumps(_engine.run_variant(_engine_inputs[key], _engine_json.loads(variant_json)), ensure_ascii=False)
`;

let runRule: ((inputsJson: string) => string) | null = null;
let runVariant: ((key: string, inputsJson: string, variantJson: string) => string) | null = null;

async function init(message: InitMessage) {
  const { loadPyodide } = await import(/* @vite-ignore */ `${message.pyodideUrl}pyodide.mjs`);
  const pyodide = await loadPyodide({ indexURL: message.pyodideUrl });
  await pyodide.loadPackage(["pydantic", "tzdata"], { messageCallback: () => undefined });
  const root = "/home/pyodide";
  for (const [path, source] of Object.entries(message.bundle.modules)) {
    const target = `${root}/${path}`;
    pyodide.FS.mkdirTree(target.slice(0, target.lastIndexOf("/")));
    pyodide.FS.writeFile(target, source, { encoding: "utf8" });
  }
  pyodide.runPython(`import sys; sys.path.insert(0, ${JSON.stringify(root)})`);
  pyodide.runPython(BRIDGE(message.bundle.entry));
  runRule = pyodide.globals.get("_engine_run_rule");
  runVariant = pyodide.globals.get("_engine_run_variant");
}

self.onmessage = async (event: MessageEvent<InMessage>) => {
  const message = event.data;
  const post = (payload: unknown) => (self as unknown as Worker).postMessage(payload);
  const started = performance.now();
  try {
    if (message.type === "init") {
      await init(message);
      post({ id: message.id, ok: true, ms: Math.round(performance.now() - started) });
      return;
    }
    if (!runRule || !runVariant) throw new Error("计算组件还没就绪");
    const text = message.type === "rule" ? runRule(message.inputsJson)
      : runVariant(message.key, message.inputsJson, JSON.stringify(message.variant));
    post({ id: message.id, ok: true, text, ms: Math.round(performance.now() - started) });
  } catch (error) {
    post({ id: message.id, ok: false, error: error instanceof Error ? `${error.name}: ${error.message}` : String(error) });
  }
};
