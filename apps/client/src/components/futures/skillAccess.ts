import type { AgentSkill, PlanFeatures } from "../../api";

/** The legacy camel feature switch → the registry skills it opened (mirrors the
 *  Runtime's FEATURE_TO_SKILLS). Only the fallback path reads this: a Runtime
 *  without GET /api/agent/skills still answers the feature view, and there the
 *  group is only as open as its switch. */
const FEATURE_SKILLS: Array<[keyof PlanFeatures, string[]]> = [
  ["aiExecution", ["ai-execution"]],
  ["webResearch", ["web-research"]],
  ["marketBriefs", ["market-briefs"]],
  ["strategies", ["strategy-management", "strategy-signals"]],
  ["backtests", ["strategy-backtests"]],
  ["replays", ["market-replay", "replay-control"]],
  ["voice", ["voice-call"]],
];

/** The unified refusal line, the same wordings the Runtime's `unavailable_line`
 *  produces for voice and typed routers — a skill blocked only through its parent
 *  names the parent (「子」随「上级」关闭 / 当前档位未开通「上级」). */
export function skillUnavailableLine(
  skill: Pick<AgentSkill, "id" | "label" | "unavailableReason">,
  fallbackLabel?: string,
  parent?: Pick<AgentSkill, "label" | "unavailableReason" | "aiFeature"> | null,
): string {
  const label = skill.label || fallbackLabel || skill.id;
  if (parent) {
    const parentLabel = parent.label || "上级";
    if (parent.unavailableReason === "disabled") return `「${label}」随「${parentLabel}」关闭`;
    if (parent.unavailableReason === "owner_only") return `「${parentLabel}」仅主账号可用`;
    return `当前档位未开通「${parentLabel}」`;
  }
  if (skill.unavailableReason === "disabled") return `「${label}」已停用`;
  if (skill.unavailableReason === "owner_only") return `「${label}」仅主账号可用`;
  return `当前档位未开通「${label}」`;
}

/** The one availability question every skill-gated button asks: null = usable,
 *  otherwise the line for the button's `title`. `skills` is the registry view
 *  (null on an old Runtime); `features` is the status payload's legacy view
 *  used as the fallback, so a pre-registry Runtime keeps graying exactly the
 *  buttons it did before. */
export function skillBlockedLine(
  id: string,
  skills: AgentSkill[] | null | undefined,
  features: PlanFeatures | undefined,
  fallbackLabel?: string,
): string | null {
  if (skills) {
    const skill = skills.find(item => item.id === id);
    if (!skill || skill.available) return null;
    // The owner's global switch only takes a non-AI skill away from the AI; the user's own buttons keep it.
    if (skill.unavailableReason === "disabled" && skill.aiFeature === false) return null;
    // A skill blocked only through its parent names the parent (same as the server's line).
    const parent = skill.parent ? skills.find(item => item.id === skill.parent) ?? null : null;
    if (parent && !parent.available) {
      if (parent.unavailableReason === "disabled" && parent.aiFeature === false) return null;
      return skillUnavailableLine(skill, fallbackLabel, parent);
    }
    return skillUnavailableLine(skill, fallbackLabel);
  }
  if (features) {
    for (const [key, ids] of FEATURE_SKILLS) {
      if (features[key] === false && ids.includes(id)) {
        return `当前档位未开通「${fallbackLabel ?? id}」`;
      }
    }
  }
  return null;
}

/** The quick-skill bar's candidates: every registry skill that offers a
 *  `prompt:` quick action, pinned or not — the settings checkbox list and the
 *  bar both start from here. */
export function quickActionSkills(skills: AgentSkill[] | null | undefined): AgentSkill[] {
  if (!skills) return [];
  return skills.filter(skill => skill.quickAction?.startsWith("prompt:"));
}
