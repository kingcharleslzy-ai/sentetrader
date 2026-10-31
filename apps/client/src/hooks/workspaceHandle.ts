import type { ConnectionState } from "./useRuntimeSession";
import type { FenceContext, IdentityFence } from "./useIdentityFence";

export type ToastKind = "success" | "info" | "warning" | "error";

/**
 * What every trading-workspace hook needs from the App: where the Runtime is,
 * whether it is connected, the identity fence that retires stale work, and the
 * toast. Hooks tag their state with `orderContext` and drop it when it changes.
 */
export interface WorkspaceHandle {
  baseUrl: string;
  connection: ConnectionState;
  sessionId: string | undefined;
  /** Non-empty while a historical replay owns the workspace. */
  replayPath: string;
  fence: IdentityFence;
  orderContext: FenceContext;
  isCurrentOrderContext: () => boolean;
  setToast: (text?: string, kind?: ToastKind) => void;
}
