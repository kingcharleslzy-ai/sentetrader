import { Fragment, useMemo } from "react";
import type { AdminPlan, AdminSkill } from "../../../api";
import { useDisclosureSurface } from "../../../uiSurfaces";
import { Switch } from "../../settings/controls";
import { SKILL_CATEGORY_ORDER, SectionHeader } from "./shared";

/** The 技能 section: the skill × tier matrix, realigned — the skill column a
 *  fixed 280px, tier columns equal and centered, group headers as in-table
 *  rows, the table header sticky inside the content scroll region. */
export function SkillsSection({ skills, plans, busySkillCells, skillRowStatus, onFlipSwitch, onTierSwitch }: {
  skills: AdminSkill[] | undefined;
  plans: AdminPlan[] | undefined;
  busySkillCells: ReadonlySet<string>;
  skillRowStatus: Record<string, string>;
  onFlipSwitch: (skill: AdminSkill, value: boolean) => void;
  onTierSwitch: (skill: AdminSkill, tierName: string, open: boolean) => void;
}) {
  // The registry arrives category-stable; one header row per run of the same category.
  const skillGroups = useMemo(() => {
    if (!skills) return undefined;
    // The registry lists skills by id, so one category's rows are scattered:
    // collect each category once (product order, unknown ones last) — a
    // repeated category would also repeat its Fragment key.
    const byCategory = new Map<string, AdminSkill[]>();
    for (const skill of skills) byCategory.set(skill.category, [...(byCategory.get(skill.category) ?? []), skill]);
    const rank = (category: string) => {
      const index = SKILL_CATEGORY_ORDER.indexOf(category);
      return index < 0 ? SKILL_CATEGORY_ORDER.length : index;
    };
    return [...byCategory.entries()]
      .sort(([left], [right]) => rank(left) - rank(right))
      .map(([category, items]) => {
        // Within the category, children sit under their parent (one level; a
        // parent outside the category or a missing one leaves the child standalone).
        const inCategory = new Set(items.map(item => item.id));
        const childrenOf = new Map<string, AdminSkill[]>();
        const roots: AdminSkill[] = [];
        for (const skill of items) {
          if (skill.parent && inCategory.has(skill.parent)) {
            childrenOf.set(skill.parent, [...(childrenOf.get(skill.parent) ?? []), skill]);
            continue;
          }
          roots.push(skill);
        }
        return { category, items: roots.map(skill => ({ skill, children: childrenOf.get(skill.id) ?? [] })) };
      });
  }, [skills]);

  if (skills === undefined || skillGroups === undefined || plans === undefined) {
    return <>
      <SectionHeader title="技能" helpKey="skills" help="全局对所有人生效（含主账号）；档位列只对该档位成员生效；“—”表示不分档位或不可设；下级技能跟随上级。" />
      <div className="fdw-admin-section-body"><p className="fdw-admin-empty">读取中</p></div>
    </>;
  }
  return <>
    <SectionHeader title="技能" helpKey="skills" help="全局对所有人生效（含主账号）；档位列只对该档位成员生效；“—”表示不分档位或不可设；下级技能跟随上级。" />
    <div className="fdw-admin-section-body fdw-admin-skills-body">
      <div className="fdw-admin-table-card">
      <table className="fdw-admin-table fdw-admin-skills" aria-label="技能开关矩阵">
        <thead><tr>
          <th>技能</th>
          <th>全局</th>
          {plans.map(plan => <th key={plan.name} className="fdw-admin-tier" title={plan.name}>
            <strong>{plan.label}</strong>
          </th>)}
        </tr></thead>
        <tbody>
          {skillGroups.map(group => <Fragment key={group.category}>
            <tr className="fdw-admin-skill-group"><th scope="colgroup" colSpan={2 + plans.length}>{group.category}</th></tr>
            {group.items.map(({ skill, children }) => <SkillBranch key={skill.id} skill={skill} childSkills={children}
              plans={plans} busySkillCells={busySkillCells} skillRowStatus={skillRowStatus}
              onFlipSwitch={onFlipSwitch} onTierSwitch={onTierSwitch} />)}
          </Fragment>)}
        </tbody>
      </table>
      </div>
    </div>
  </>;
}

/** One skill's row(s) in the 技能 matrix: the skill first, then — for a parent —
 *  its children under an expand toggle (collapsed by default; the toggle is a
 *  commandable disclosure so the AI's `disclosure` command can open it too).
 *  Children follow their parent: with the parent's global switch off, or in a
 *  tier that does not open a tier-gated parent, the child's switches disable. */
function SkillBranch({ skill, childSkills, plans, busySkillCells, skillRowStatus, onFlipSwitch, onTierSwitch }: {
  skill: AdminSkill;
  childSkills: AdminSkill[];
  plans: AdminPlan[];
  busySkillCells: ReadonlySet<string>;
  skillRowStatus: Record<string, string>;
  onFlipSwitch: (skill: AdminSkill, value: boolean) => void;
  onTierSwitch: (skill: AdminSkill, tierName: string, open: boolean) => void;
}) {
  const [expanded, setExpanded] = useDisclosureSurface("admin-skill-children", {
    id: skill.id, available: childSkills.length > 0,
  });
  const parentOff = !skill.enabled;
  const renderRow = (item: AdminSkill, child: boolean) => <tr key={item.id}
    className={child ? "fdw-admin-skill-child" : undefined}>
    <th>
      <strong>{item.label}</strong>
      <small className="fdw-admin-skill-desc">{item.summary}</small>
      {child && parentOff ? <small className="fdw-admin-skill-follow">随「{skill.label}」关闭</small> : null}
      {skillRowStatus[item.id] ? <span role="status" className="fdw-admin-cell-status">{skillRowStatus[item.id]}</span> : null}
      {!child && childSkills.length > 0 ? (
        <button type="button" className="fdw-admin-skill-expand" aria-expanded={expanded}
          title={`${skill.label}的下级技能`}
          onClick={() => setExpanded(!expanded)}>{expanded ? "▾" : "▸"} 含 {childSkills.length} 项</button>
      ) : null}
    </th>
    <td><Switch label={`${item.label} 全局`} checked={item.enabled}
      disabled={busySkillCells.has(`${item.id}@global`) || (child && parentOff)}
      onChange={value => onFlipSwitch(item, value)} /></td>
    {plans.map(plan => <td key={plan.name}>
      {plan.name === "guest" && item.id !== "voice-call"
        ? <span aria-label="游客不可用">—</span>
        : item.tierGated && !item.ownerOnly
          ? <Switch label={`${item.label} ${plan.label}`} checked={item.tiers[plan.name] === true}
            disabled={busySkillCells.has(`${item.id}@${plan.name}`)
              || (child && (parentOff || (skill.tierGated && skill.tiers[plan.name] !== true)))}
            onChange={value => onTierSwitch(item, plan.name, value)} />
          : <span aria-label="不适用">—</span>}
    </td>)}
  </tr>;
  return <>{renderRow(skill, false)}{expanded ? childSkills.map(item => renderRow(item, true)) : null}</>;
}
