/** Desktop-only: append a line to the shell's frontend.log so a machine we cannot see can be diagnosed. */
export function logDesktop(kind: string, message: string, detail = "") {
  if (typeof window === "undefined" || !("__TAURI_INTERNALS__" in window)) return;
  import("@tauri-apps/api/core").then(({ invoke }) => invoke("desktop_frontend_error", { error: { kind, message, detail } })).catch(() => undefined);
}
