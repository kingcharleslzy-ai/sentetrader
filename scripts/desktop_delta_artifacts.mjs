import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const hash = (path) => createHash("sha256").update(readFileSync(path)).digest("hex");
const compareVersions = (a, b) => {
  const left = a.split(".").map(Number), right = b.split(".").map(Number);
  for (let i = 0; i < 3; i++) if (left[i] !== right[i]) return left[i] - right[i];
  return 0;
};

export function baseArtifacts(root, version) {
  if (!existsSync(root)) return [];
  return readdirSync(root).filter(v => /^\d+\.\d+\.\d+$/.test(v) && compareVersions(v, version) < 0)
    .sort((a, b) => compareVersions(b, a)).slice(0, 2).flatMap(v => {
      const mac = join(root, v, `AI-Trader_${v}_aarch64.app.tar.gz`);
      const windows = join(root, v, `AI-Trader_${v}_x64-setup.exe`);
      if (!existsSync(mac) || !existsSync(windows)) return [];
      const macDigest = hash(mac), windowsDigest = hash(windows);
      return [{ version: v, mac, windows, macDigest, windowsDigest,
        release: `${v}-${createHash("sha256").update(`${macDigest}:${windowsDigest}`).digest("hex").slice(0, 16)}` }];
    });
}

export function prepareDeltaArtifacts({ version, platform, artifact, bases, staging, deltaTool, run, sign }) {
  const payload = { schemaVersion: 1, version, sha256: hash(artifact), size: statSync(artifact).size, patches: [] };
  const files = [];
  for (const base of bases) {
    const basePath = platform === "darwin-aarch64" ? base.mac : base.windows;
    const baseSha256 = platform === "darwin-aarch64" ? base.macDigest : base.windowsDigest;
    const file = `${baseSha256}.bsdiff`;
    const output = join(staging, file);
    run(deltaTool, ["diff", basePath, artifact, output]);
    if (statSync(output).size >= payload.size * 0.9) {
      unlinkSync(output);
      continue;
    }
    const restored = join(staging, `${baseSha256}.restored`);
    try {
      run(deltaTool, ["patch", basePath, output, restored]);
      if (hash(restored) !== payload.sha256) throw new Error("desktop_delta_roundtrip_failed");
    } finally { if (existsSync(restored)) unlinkSync(restored); }
    payload.patches.push({ baseSha256, baseVersion: base.version, baseRelease: base.release,
      file, sha256: hash(output), size: statSync(output).size });
    files.push(output);
  }
  const descriptor = join(staging, `${platform}-integrity.json`);
  writeFileSync(descriptor, JSON.stringify(payload));
  sign(descriptor);
  return { files, integrity: { payload: readFileSync(descriptor).toString("base64"),
    signature: readFileSync(`${descriptor}.sig`, "utf8").trim() }, patches: payload.patches };
}
