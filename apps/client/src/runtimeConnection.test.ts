// @vitest-environment node
import { afterEach, describe, expect, test, vi } from "vitest";
import { cloudRuntimeAuthMode, defaultRuntimeMode, validateCloudRuntimeUrl } from "./runtimeConnection";

afterEach(() => vi.unstubAllEnvs());

describe("cloud runtime endpoint", () => {
  test("packaged build without an explicit endpoint stays disabled", () => {
    expect(validateCloudRuntimeUrl(undefined)).toBeUndefined();
    expect(validateCloudRuntimeUrl("")).toBeUndefined();
  });

  test("requires HTTPS except for an explicitly enabled localhost development endpoint", () => {
    expect(validateCloudRuntimeUrl("https://runtime.example.cn/")).toBe("https://runtime.example.cn");
    expect(validateCloudRuntimeUrl("http://runtime.example.cn")).toBeUndefined();
    expect(validateCloudRuntimeUrl("http://127.0.0.1:8000")).toBeUndefined();
    expect(validateCloudRuntimeUrl("http://127.0.0.1:8000", true)).toBe("http://127.0.0.1:8000");
    expect(validateCloudRuntimeUrl("https://user:pass@runtime.example.cn")).toBeUndefined();
  });

  test("cloud image can default to cloud only when a valid endpoint is baked in", () => {
    vi.stubEnv("VITE_DEFAULT_RUNTIME_MODE", "cloud");
    vi.stubEnv("VITE_CLOUD_RUNTIME_URL", "https://runtime.example.cn");
    expect(defaultRuntimeMode()).toBe("cloud");
    vi.stubEnv("VITE_CLOUD_RUNTIME_URL", "");
    expect(defaultRuntimeMode()).toBe("local");
  });

  test("uses owner login by default while preserving explicit test and OIDC modes", () => {
    expect(cloudRuntimeAuthMode()).toBe("owner-password");
    vi.stubEnv("VITE_CLOUD_AUTH_MODE", "local-token");
    expect(cloudRuntimeAuthMode()).toBe("local-token");
    vi.stubEnv("VITE_CLOUD_AUTH_MODE", "cloud-jwt");
    expect(cloudRuntimeAuthMode()).toBe("cloud-jwt");
  });
});
