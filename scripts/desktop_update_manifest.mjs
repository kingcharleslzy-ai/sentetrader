export const DEFAULT_UPDATE_NOTES = "AI Trader 1.0 TqKq 模拟期货客户端。实盘和自动交易保持关闭。";

export function parseDesktopUpdateArguments(args) {
  const result = {
    notes: DEFAULT_UPDATE_NOTES,
    windowsInstaller: undefined,
    macCandidate: undefined,
    verifyOnly: false,
  };
  for (let index = 0; index < args.length; index += 2) {
    const flag = args[index];
    const value = args[index + 1]?.trim();
    if (!value) throw new Error("desktop_update_arguments_invalid");
    if (flag === "--notes") result.notes = value;
    else if (flag === "--windows-installer") result.windowsInstaller = value;
    else if (flag === "--mac-candidate") result.macCandidate = value;
    else if (flag === "--verify-only") result.verifyOnly = value === "1";
    else throw new Error("desktop_update_arguments_invalid");
  }
  if (!result.windowsInstaller) {
    throw new Error("desktop_update_windows_artifacts_required");
  }
  return result;
}

export function buildDesktopUpdateManifest({
  version,
  notes,
  pubDate,
  publicBase,
  releaseId,
  darwinArtifactName,
  darwinSignature,
  windowsArtifactName,
  windowsSignature,
}) {
  for (const value of [
    version, notes, pubDate, publicBase, releaseId,
    darwinArtifactName, darwinSignature, windowsArtifactName, windowsSignature,
  ]) {
    if (!String(value || "").trim()) throw new Error("desktop_update_manifest_value_missing");
  }
  return {
    version,
    notes,
    pub_date: pubDate,
    platforms: {
      "darwin-aarch64": {
        signature: darwinSignature,
        url: `${publicBase}/releases/${releaseId}/${darwinArtifactName}`,
      },
      "windows-x86_64": {
        signature: windowsSignature,
        url: `${publicBase}/releases/${releaseId}/${windowsArtifactName}`,
      },
    },
  };
}
