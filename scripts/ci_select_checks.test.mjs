import assert from "node:assert/strict";
import test from "node:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { planChecks } from "./ci_select_checks.mjs";

const RT = "services/trader-runtime";

// A minimal two-service tree: planChecks reads only src/ and tests/ of both services.
function fixture(files) {
  const root = mkdtempSync(join(tmpdir(), "ci-select-"));
  const all = {
    [`${RT}/src/trader_runtime/__init__.py`]: "",
    [`${RT}/tests/conftest.py`]: "",
    "services/tqkq-sidecar/src/tqkq_readonly_sidecar/__init__.py": "",
    "services/tqkq-sidecar/tests/conftest.py": "",
    ...files,
  };
  for (const [path, text] of Object.entries(all)) {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), text);
  }
  return root;
}

function runtimeTests(root, paths) {
  try {
    return planChecks(paths, { root }).python.runtime?.tests;
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test("a module run with `python -m` selects the tests that launch it", () => {
  const root = fixture({
    [`${RT}/src/trader_runtime/entry.py`]: "def main():\n    pass\n",
    [`${RT}/tests/test_entry_import.py`]: "from trader_runtime.entry import main\n",
    [`${RT}/tests/test_entry_process.py`]:
      'import sys\nARGS = [sys.executable, "-m", "trader_runtime.entry", "mcp"]\n',
  });
  assert.deepEqual(runtimeTests(root, [`${RT}/src/trader_runtime/entry.py`]), [
    `${RT}/tests/test_entry_import.py`,
    `${RT}/tests/test_entry_process.py`,
  ]);
});

test("a script run by file path selects the tests of its launcher", () => {
  const root = fixture({
    [`${RT}/src/trader_runtime/worker.py`]: "print('work')\n",
    [`${RT}/src/trader_runtime/runner.py`]:
      'from pathlib import Path\nWORKER = Path(__file__).with_name("worker.py")\n',
    [`${RT}/tests/test_runner.py`]: "from trader_runtime.runner import WORKER\n",
  });
  assert.deepEqual(runtimeTests(root, [`${RT}/src/trader_runtime/worker.py`]), [`${RT}/tests/test_runner.py`]);
});

test("a rename listed as deletion plus addition runs the whole area", () => {
  // `git diff --no-renames` lists both sides; tests still importing the old name must run.
  const root = fixture({
    [`${RT}/src/trader_runtime/renamed.py`]: "VALUE = 1\n",
    [`${RT}/tests/test_old_name.py`]: "from trader_runtime.original import VALUE\n",
  });
  assert.equal(runtimeTests(root, [
    `${RT}/src/trader_runtime/original.py`,
    `${RT}/src/trader_runtime/renamed.py`,
  ]), null);
});

test("a nested tests/ helper selects the tests that import it, not the tests that only name its directory", () => {
  const root = fixture({
    [`${RT}/tests/fixtures/fake_server.py`]: "class FakeServer:\n    pass\n",
    [`${RT}/tests/fixtures/data.json`]: "{}\n",
    [`${RT}/tests/test_top_import.py`]: "from fixtures.fake_server import FakeServer\n",
    [`${RT}/tests/test_lazy_import.py`]: "def test_it():\n    from fixtures.fake_server import FakeServer\n",
    [`${RT}/tests/test_package_import.py`]: "from fixtures import fake_server\n",
    // Names the fixtures directory, never imports the helper: must not stand in for the importers.
    [`${RT}/tests/test_reads_data.py`]: 'from pathlib import Path\nDATA = Path(__file__).parent / "fixtures" / "data.json"\n',
  });
  assert.deepEqual(runtimeTests(root, [`${RT}/tests/fixtures/fake_server.py`]), [
    `${RT}/tests/test_lazy_import.py`,
    `${RT}/tests/test_package_import.py`,
    `${RT}/tests/test_top_import.py`,
  ]);
});

test("a nested tests/ helper nothing imports runs the whole area", () => {
  const root = fixture({
    [`${RT}/tests/fixtures/loose_helper.py`]: "VALUE = 1\n",
    [`${RT}/tests/test_reads_dir.py`]: 'DATA = "fixtures"\nOTHER = "/" + "fixtures"\n',
  });
  assert.equal(runtimeTests(root, [`${RT}/tests/fixtures/loose_helper.py`]), null);
});

test("a subprocess launch target the scan cannot resolve runs the whole area", () => {
  const files = {
    [`${RT}/src/trader_runtime/entry.py`]: "def main():\n    pass\n",
    [`${RT}/src/trader_runtime/other.py`]: "VALUE = 1\n",
    [`${RT}/tests/test_other.py`]: "from trader_runtime.other import VALUE\n",
  };
  const changed = [`${RT}/src/trader_runtime/other.py`];
  // Control: literal targets stay resolvable and keep the selection partial.
  assert.deepEqual(runtimeTests(fixture({
    ...files,
    [`${RT}/tests/test_launch.py`]: 'import sys\nARGS = [sys.executable, "-m", "trader_runtime.entry"]\n',
  }), changed), [`${RT}/tests/test_other.py`]);
  const unresolved = {
    variable: 'import sys\nMODULE = "trader_runtime.entry"\nARGS = [sys.executable, "-m", MODULE]\n',
    fstring: 'import sys\ndef args(name):\n    return [sys.executable, "-m", f"trader_runtime.{name}"]\n',
    template: 'COMMAND = "python -m {module} serve"\n',
    "built .py name": 'import subprocess\ndef run(name):\n    subprocess.run(["python", f"{name}.py"])\n',
    "concatenated .py name": 'import sys\nSCRIPT = "worker" + ".py"\nARGS = [sys.executable, SCRIPT]\n',
    // #932 X2: a name held in a variable or constant, then + ".py".
    "variable plus .py": 'import sys\nNAME = "worker"\nARGS = [sys.executable, NAME + ".py"]\n',
    "path name plus .py": 'import subprocess\nfrom pathlib import Path\ndef run(stem):\n    subprocess.run(["python", Path(__file__).with_name(stem + "_job.py")])\n',
  };
  for (const [name, text] of Object.entries(unresolved)) {
    const root = fixture({ ...files, [`${RT}/tests/test_launch.py`]: text });
    assert.equal(runtimeTests(root, changed), null, name);
  }
});

test("help/catalog.json selects its three real consumers; catalog plus its loader still widens", () => {
  const catalog = `${RT}/src/trader_runtime/help/catalog.json`;
  const loader = `${RT}/src/trader_runtime/app_help.py`;
  const consumers = ["test_app_help", "test_skill_registry", "test_strategy_plain"].map((name) => `${RT}/tests/${name}.py`);
  assert.deepEqual(planChecks([catalog]).python.runtime.tests, consumers);
  const widened = planChecks([catalog, loader]).python.runtime.tests;
  assert.ok(widened === null || widened.length > consumers.length);
  assert.ok(widened === null || consumers.every((file) => widened.includes(file)));
});
