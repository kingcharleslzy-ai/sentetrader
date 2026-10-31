import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptRoot = dirname(fileURLToPath(import.meta.url));
const defaultRoot = resolve(scriptRoot, "..");
const signingVariables = [
  "APPLE_CERTIFICATE",
  "APPLE_CERTIFICATE_PASSWORD",
  "APPLE_SIGNING_IDENTITY",
  "APPLE_ID",
  "APPLE_PASSWORD",
  "APPLE_TEAM_ID",
];
const forbiddenExecutionSymbols = /\b(insert_order|cancel_order|submit_live_order|liquidate_account)\b/;
const forbiddenRealTradingSymbols = /\b(TqAccount|TqCtp|TargetPosTask)\b/;
const simulationGatewayFile = "services/tqkq-sidecar/src/tqkq_readonly_sidecar/main.py";
const historicalSimulationGatewayFile = "services/tqkq-sidecar/src/tqkq_readonly_sidecar/replay.py";
// 本站模拟盘撮合引擎 (2026-10-03): calls the local SimTrade matcher's insert_order /
// cancel_order like the replay does — never a broker. It may not name a live order
// path or any real/broker account type (TqKq included: its data feed is data-only).
const paperEngineFiles = new Set([
  "services/tqkq-sidecar/src/tqkq_readonly_sidecar/paper.py",
  "services/tqkq-sidecar/src/tqkq_readonly_sidecar/paper_book.py",
]);
const paperEngineForbidden = /\b(submit_live_order|liquidate_account|TqKq|TqAccount|TqCtp|TargetPosTask)\b/;
const officialBacktestFile = "services/tqkq-sidecar/src/tqkq_readonly_sidecar/tqsdk_backtest.py";
const authorizedSimulationOrchestrationFiles = new Set([
  "services/trader-runtime/src/trader_runtime/autonomy.py",
  "services/trader-runtime/src/trader_runtime/trader_tools.py",
  // 价差两腿执行器 (2026-09-29): cancels / re-sends legs only through the simulation autonomy service.
  "services/trader-runtime/src/trader_runtime/spread_execution.py",
]);

// 外盘 MT5 行情桥 (2026-10-18, #976): market data only. Its code and deploy scripts may not
// name a terminal trading / order-calculation function or log in to an account (tests/ hold the
// fake terminal that asserts the same and are not shipped).
const mt5BridgeDirectories = ["services/mt5-bridge/src", "services/mt5-bridge/deploy"];
const mt5BridgeForbidden = /\b(order_send|order_check|order_calc\w*)\b|\blogin\s*\(/;

function requireCondition(condition, code) {
  if (!condition) throw new Error(code);
}

function json(root, relative) {
  return JSON.parse(readFileSync(join(root, relative), "utf8"));
}

function cargoVersion(root) {
  const manifest = readFileSync(join(root, "apps/desktop/src-tauri/Cargo.toml"), "utf8");
  const match = manifest.match(/^version\s*=\s*"([^"]+)"/m);
  requireCondition(match, "desktop_cargo_version_missing");
  return match[1];
}

function sourceFiles(root, relative) {
  const current = join(root, relative);
  const output = [];
  for (const entry of readdirSync(current)) {
    const path = join(current, entry);
    const child = `${relative}/${entry}`;
    if (statSync(path).isDirectory()) output.push(...sourceFiles(root, child));
    else if ([".py", ".rs", ".ts", ".tsx"].includes(extname(entry))) output.push(child);
  }
  return output;
}

export function validateWindowsReleaseConfiguration(root = defaultRoot) {
  const windows = json(root, "apps/desktop/src-tauri/tauri.windows.conf.json");
  requireCondition(
    windows.bundle?.targets?.length === 1 && windows.bundle.targets[0] === "nsis",
    "desktop_windows_nsis_target_missing",
  );
  requireCondition(
    windows.bundle?.windows?.webviewInstallMode?.type === "embedBootstrapper",
    "desktop_windows_webview_bootstrapper_missing",
  );
  const nsis = windows.bundle?.windows?.nsis;
  requireCondition(nsis?.installMode === "currentUser", "desktop_windows_current_user_install_missing");
  requireCondition(
    nsis?.languages?.includes("SimpChinese") && nsis?.languages?.includes("English"),
    "desktop_windows_installer_languages_missing",
  );
  requireCondition(
    nsis?.displayLanguageSelector === false,
    "desktop_windows_silent_language_selection_missing",
  );
  requireCondition(nsis?.installerIcon === "icons/icon.ico", "desktop_windows_installer_icon_missing");
  requireCondition(
    windows.plugins?.updater?.windows?.installMode === "passive",
    "desktop_windows_updater_install_mode_invalid",
  );
  return { targets: windows.bundle.targets, installMode: nsis.installMode };
}

function bridgeFiles(root, relative) {
  const current = join(root, relative);
  if (!existsSync(current)) return [];
  return readdirSync(current).flatMap((entry) => {
    const child = `${relative}/${entry}`;
    if (entry.startsWith(".") || entry === "__pycache__") return [];
    if (statSync(join(current, entry)).isDirectory()) return bridgeFiles(root, child);
    return [".py", ".ps1"].includes(extname(entry)) ? [child] : [];
  });
}

export function validateMt5BridgeReadOnly(root = defaultRoot) {
  for (const file of mt5BridgeDirectories.flatMap((relative) => bridgeFiles(root, relative))) {
    requireCondition(
      !mt5BridgeForbidden.test(readFileSync(join(root, file), "utf8")),
      `mt5_bridge_trading_symbol_forbidden:${file}`,
    );
  }
}

export function validateReleaseConfiguration(root = defaultRoot) {
  const rootPackage = json(root, "package.json");
  const clientPackage = json(root, "apps/client/package.json");
  const desktopPackage = json(root, "apps/desktop/package.json");
  const tauri = json(root, "apps/desktop/src-tauri/tauri.conf.json");
  const updater = json(root, "apps/desktop/src-tauri/tauri.updater.conf.json");
  const windows = validateWindowsReleaseConfiguration(root);
  const versions = new Set([
    rootPackage.version,
    clientPackage.version,
    desktopPackage.version,
    tauri.version,
    cargoVersion(root),
  ]);
  requireCondition(versions.size === 1, "desktop_release_version_mismatch");
  requireCondition(tauri.bundle?.active === true, "desktop_bundle_disabled");
  requireCondition(tauri.bundle.targets?.includes("app"), "desktop_app_bundle_missing");
  requireCondition(tauri.bundle.targets?.includes("dmg"), "desktop_dmg_bundle_missing");
  requireCondition(tauri.bundle.createUpdaterArtifacts === false, "desktop_normal_build_must_not_require_updater_key");
  requireCondition(updater.bundle?.createUpdaterArtifacts === true, "desktop_updater_artifacts_disabled");
  requireCondition(tauri.bundle.macOS?.signingIdentity === "-", "desktop_adhoc_signing_missing");
  requireCondition(tauri.bundle.macOS?.entitlements === "Entitlements.plist", "desktop_sidecar_entitlements_missing");
  requireCondition(
    readFileSync(join(root, "apps/desktop/src-tauri/Entitlements.plist"), "utf8")
      .includes("com.apple.security.cs.disable-library-validation"),
    "desktop_sidecar_library_validation_entitlement_missing",
  );
  const macInfo = readFileSync(join(root, "apps/desktop/src-tauri/Info.plist"), "utf8");
  // The real-time call only needs the microphone; system speech recognition left with 1.6.0.
  requireCondition(macInfo.includes("NSMicrophoneUsageDescription"), "desktop_mac_voice_usage_descriptions_missing");
  requireCondition(Boolean(tauri.plugins?.updater?.pubkey), "desktop_updater_public_key_missing");
  requireCondition(
    tauri.plugins?.updater?.endpoints?.length === 1
      && new URL(tauri.plugins.updater.endpoints[0]).protocol === "https:",
    "desktop_updater_https_endpoint_missing",
  );
  requireCondition(
    tauri.bundle.externalBin?.includes("binaries/ai-trader-runtime")
      && !tauri.bundle.externalBin?.includes("binaries/ai-trader-tqkq-sidecar"),
    "desktop_bundled_binaries_unexpected",
  );
  const sidecarManifest = readFileSync(
    join(root, "services/tqkq-sidecar/pyproject.toml"),
    "utf8",
  );
  requireCondition(
    sidecarManifest.includes('"python-socks[asyncio]==2.8.2"'),
    "desktop_tqkq_socks_proxy_support_missing",
  );
  requireCondition(
    tauri.bundle.longDescription?.includes("实盘交易始终关闭")
      && tauri.bundle.longDescription?.includes("Runtime 重新校验行情、账户、持仓和风险规则"),
    "desktop_bounded_simulation_disclosure_missing",
  );
  for (const relative of [
    "services/trader-runtime/src",
    "services/tqkq-sidecar/src",
    "apps/desktop/src-tauri/src",
  ]) {
    for (const file of sourceFiles(root, relative)) {
      const source = readFileSync(join(root, file), "utf8");
      requireCondition(
        file === simulationGatewayFile || authorizedSimulationOrchestrationFiles.has(file)
          || (file === historicalSimulationGatewayFile
            && !/\b(submit_live_order|liquidate_account)\b/.test(source))
          || (paperEngineFiles.has(file) && !paperEngineForbidden.test(source))
          || !forbiddenExecutionSymbols.test(source),
        `desktop_live_trading_symbol_forbidden:${file}`,
      );
      requireCondition(
        file === officialBacktestFile || !forbiddenRealTradingSymbols.test(source),
        `desktop_real_trading_symbol_forbidden:${file}`,
      );
    }
  }
  validateMt5BridgeReadOnly(root);
  const gatewaySource = readFileSync(join(root, simulationGatewayFile), "utf8");
  requireCondition(gatewaySource.includes("TqApi(TqKq()"), "desktop_tqkq_simulation_account_missing");
  requireCondition(
    gatewaySource.includes('offset=request["offset"]') &&
      gatewaySource.includes('{"OPEN", "CLOSE", "CLOSETODAY"}'),
    "desktop_explicit_offset_boundary_missing",
  );
  requireCondition(
    (gatewaySource.match(/self\.api\.insert_order\(/g) || []).length === 1 &&
      (gatewaySource.match(/self\.api\.cancel_order\(/g) || []).length === 1,
    "desktop_thin_simulation_gateway_boundary_invalid",
  );
  requireCondition(!gatewaySource.includes("TargetPosTask"), "desktop_target_position_task_forbidden");
  const autonomySource = readFileSync(
    join(root, "services/trader-runtime/src/trader_runtime/autonomy.py"), "utf8",
  );
  requireCondition(
    autonomySource.includes("AutonomousSimulationService")
      && autonomySource.includes("ai_execution_not_active")
      && autonomySource.includes("authorization_order_limit_reached")
      && autonomySource.includes("UNKNOWN")
      && autonomySource.includes('"liveTrading": "disabled"')
      && autonomySource.includes("self._action_lock"),
    "desktop_bounded_ai_simulation_boundary_missing",
  );
  requireCondition(
    !/\b(TqApi|TqAccount|TqCtp|TargetPosTask)\b/.test(autonomySource)
      && !autonomySource.includes(".insert_order("),
    "desktop_ai_simulation_direct_gateway_forbidden",
  );
  const backtestSource = readFileSync(join(root, officialBacktestFile), "utf8");
  requireCondition(
    backtestSource.includes("TqSim") && backtestSource.includes("TqBacktest") &&
      backtestSource.includes("TargetPosTask") && backtestSource.includes("account=sim"),
    "desktop_official_backtest_boundary_missing",
  );
  requireCondition(
    !/\b(TqKq|TqAccount|TqCtp)\b/.test(backtestSource) &&
      !forbiddenExecutionSymbols.test(backtestSource),
    "desktop_backtest_real_account_forbidden",
  );
  for (const relative of [
    "apps/client/src/priceAlerts.ts",
    "apps/client/src/components/MarketWorkbench.tsx",
  ]) {
    // Removed features leave the scan list stale; present files are still enforced.
    if (!existsSync(join(root, relative))) continue;
    const reminderSource = readFileSync(join(root, relative), "utf8");
    requireCondition(
      !forbiddenExecutionSymbols.test(reminderSource)
        && !forbiddenRealTradingSymbols.test(reminderSource),
      `desktop_price_alert_execution_forbidden:${relative}`,
    );
  }
  return {
    version: rootPackage.version,
    targets: tauri.bundle.targets,
    windowsTargets: windows.targets,
  };
}

export function validateSigningEnvironment(env) {
  // Local builds without Apple credentials stay ad-hoc; once an identity is configured it must be
  // the Developer ID one the CI build signs with and the publisher's fail-closed checks require.
  const identity = env.APPLE_SIGNING_IDENTITY?.trim();
  if (identity) {
    requireCondition(identity !== "-" && identity.startsWith("Developer ID Application:"),
      "desktop_signing_identity_must_be_developer_id");
    const missing = signingVariables.filter((name) => !env[name]);
    requireCondition(missing.length === 0, `desktop_signing_environment_missing:${missing.join(",")}`);
  }
  const cloudRuntimeUrl = env.VITE_CLOUD_RUNTIME_URL?.trim();
  if (cloudRuntimeUrl) {
    let endpoint;
    try {
      endpoint = new URL(cloudRuntimeUrl);
    } catch {
      throw new Error("desktop_cloud_endpoint_must_be_https");
    }
    requireCondition(
      endpoint.protocol === "https:" && !endpoint.username && !endpoint.password &&
        !endpoint.search && !endpoint.hash,
      "desktop_cloud_endpoint_must_be_https",
    );
  }
}

export function runReleasePreflight({ root = defaultRoot, env = process.env, configurationOnly = false } = {}) {
  const configuration = validateReleaseConfiguration(root);
  if (!configurationOnly) validateSigningEnvironment(env);
  return {
    status: "ready",
    version: configuration.version,
    targets: configuration.targets,
    windowsTargets: configuration.windowsTargets,
    signingRequired: !configurationOnly,
    liveTrading: "disabled",
  };
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (invoked) {
  const result = runReleasePreflight({
    configurationOnly: process.argv.includes("--configuration-only"),
  });
  process.stdout.write(`${JSON.stringify(result)}\n`);
}
