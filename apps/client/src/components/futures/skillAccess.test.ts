// @vitest-environment node
import { expect, test } from "vitest";
import type { AgentSkill } from "../../api";
import { skillBlockedLine } from "./skillAccess";

const skill = (id: string, reason: AgentSkill["unavailableReason"], aiFeature?: boolean, parent?: string | null) =>
  ({ id, label: id, available: reason === null, unavailableReason: reason, aiFeature, parent }) as AgentSkill;

test("后台关掉非 AI 技能只影响 AI，用户自己的按钮照常可用", () => {
  expect(skillBlockedLine("order-proposal", [skill("order-proposal", "disabled", false)], undefined)).toBeNull();
  expect(skillBlockedLine("voice-call", [skill("voice-call", "disabled", true)], undefined)).toBe("「voice-call」已停用");
  // Tier limits still apply to the user; an older Runtime without aiFeature keeps the old behavior.
  expect(skillBlockedLine("market-replay", [skill("market-replay", "not_in_plan", false)], undefined)).toBe("当前档位未开通「market-replay」");
  expect(skillBlockedLine("order-proposal", [skill("order-proposal", "disabled")], undefined)).toBe("「order-proposal」已停用");
});

test("只因上级不可用的技能，原因行说上级（与服务端同口径）", () => {
  const registry = [
    // The registry view carries the inherited reason on the child itself (availability_map), plus its parent.
    skill("strategy-management", "disabled", true),
    skill("strategy-backtests", "disabled", true, "strategy-management"),
    skill("order-proposal", "not_in_plan", false),
    skill("batch-close", "not_in_plan", false, "order-proposal"),
    // A blocked-in-its-own-right child keeps its own line.
    skill("chart-analysis", null, true),
    skill("chart-annotations", "owner_only", true, "chart-analysis"),
  ];
  expect(skillBlockedLine("strategy-backtests", registry, undefined)).toBe("「strategy-backtests」随「strategy-management」关闭");
  expect(skillBlockedLine("batch-close", registry, undefined)).toBe("当前档位未开通「order-proposal」");
  expect(skillBlockedLine("chart-annotations", registry, undefined)).toBe("「chart-annotations」仅主账号可用");
  // The parent's non-AI disabled switch does not take the user's own button away.
  const nonAi = [skill("order-proposal", "disabled", false), skill("batch-close", "disabled", false, "order-proposal")];
  expect(skillBlockedLine("batch-close", nonAi, undefined)).toBeNull();
});
