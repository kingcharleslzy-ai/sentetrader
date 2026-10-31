import type { RoutineStepView } from "@ai-trader/ui-events";
import { requestJson } from "./api";

/** 我的流程 (2026-10-06): the user's named routines (GET /api/routines). Saving is the user's own 保存
 *  on a 流程卡; running sends “跑一下<名称>” as the user's message, with the routine's id so the Runtime
 *  runs exactly the one picked (#522: another routine may be named “跑一下<名称>”); editing goes back
 *  through the chat. */
export interface RoutineItem {
  id: string;
  name: string;
  steps: RoutineStepView[];
  stepCount: number;
  createdAt: string;
  updatedAt: string;
  lastRunAt: string | null;
}

export interface RoutineList { items: RoutineItem[]; limits: { routines: number; steps: number; nameChars: number } }

/** Fired after a save or delete so an open 我的流程 list rereads. */
export const ROUTINES_CHANGED = "ai-trader:routines-changed";

export const routineRunPrompt = (name: string) => `跑一下${name}`;

/** The routine a 运行 picks: its id runs it, its name is what the chat shows. */
export type RoutineRef = Pick<RoutineItem, "id" | "name">;

/** The route's Chinese refusal (`detail`), else the fallback. */
export function routineError(cause: unknown, fallback: string): string {
  const detail = (cause as { detail?: { detail?: unknown } } | undefined)?.detail?.detail;
  return typeof detail === "string" && detail ? detail : fallback;
}

export function listRoutines(baseUrl: string) {
  return requestJson<RoutineList>(`${baseUrl}/api/routines`);
}

/** 流程卡's 保存: throws an Error carrying the route's sentence (已有 5 个、重名、被拒…). */
export async function saveRoutineDraft(baseUrl: string, draftId: string): Promise<void> {
  try {
    await requestJson(`${baseUrl}/api/routines`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ draftId }),
    });
  } catch (cause) {
    throw new Error(routineError(cause, "没有保存，请重试"), { cause });
  }
  window.dispatchEvent(new Event(ROUTINES_CHANGED));
}

export async function deleteRoutine(baseUrl: string, routineId: string): Promise<void> {
  await requestJson(`${baseUrl}/api/routines/${encodeURIComponent(routineId)}`, { method: "DELETE" });
  window.dispatchEvent(new Event(ROUTINES_CHANGED));
}
