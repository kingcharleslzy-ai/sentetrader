import { Bell, BookOpenText, BriefcaseBusiness, ListTree, MessageSquareText, ReceiptText, Settings2, Download } from "lucide-react";
import { useState, type Dispatch, type SetStateAction } from "react";
import type { WorkspaceAccess } from "../../hooks/useAccountAccess";
import { isTauriRuntime } from "../../runtimeConnection";
import { RailIndicator } from "../RailIndicator";
import "./ToolsRail.css";
import type { RightDock } from "./support";

interface Props {
  access?: WorkspaceAccess;
  rightDock: RightDock;
  setRightDock: Dispatch<SetStateAction<RightDock>>;
  /** The order panel only exists on the trade view. */
  onTrade: () => void;
  onSettings: () => void;
  settingsOpen?: boolean;
  /** M2: why the 策略 skill is unavailable (skill registry reason line); null = usable.
   *  Opening is refused, closing an open panel stays possible. */
  strategyBlocked?: string | null;
}

/** The vertical rail (owner 2026-10-25): 对话 / 调研 / 策略 / 提醒 / 下单 / 持仓 / 下载 / 设置; one panel is open at a time.
 *  调研 was 新闻 and holds the 辩论 page as one of its tabs (the panel id stays `news`); 提醒 holds 我的提醒,
 *  通用提醒 (was 设置 → 提醒) and 提醒记录 (was the bell's 提醒中心 popover). */
export function ToolsRail({ rightDock, setRightDock, onTrade, onSettings, settingsOpen = false, strategyBlocked, access }: Props) {
  const [blocked, setBlocked] = useState("");
  const web = !isTauriRuntime();
  const allowed = (key: string) => access?.require(key) ?? true;
  const toggle = (dock: Exclude<RightDock, null>) => setRightDock((current) => current === dock ? null : dock);
  return (
    <nav className="fdw-tools-rail" aria-label="辅助工具" data-agent-target="rail">
      <RailIndicator selection={String(rightDock)}/>
      <button type="button" className={rightDock === "ai" ? "active" : ""} aria-label={rightDock === "ai" ? "收起对话" : "打开对话"} aria-expanded={rightDock === "ai"} data-agent-target="rail:ai" onClick={() => { if (rightDock === "ai" || allowed("ai-chat")) toggle("ai"); }}><MessageSquareText /><span>对话</span></button>
      <i className="fdw-rail-sep" aria-hidden="true" />
      <button type="button" className={rightDock === "news" ? "active" : ""} aria-label={rightDock === "news" ? "收起调研面板" : "打开调研面板"} aria-expanded={rightDock === "news"} data-agent-target="rail:news" onClick={() => toggle("news")}><BookOpenText /><span>调研</span></button>
      <button type="button" className={rightDock === "strategy" ? "active" : ""} aria-label={rightDock === "strategy" ? "收起策略面板" : "打开策略面板"} aria-expanded={rightDock === "strategy"} data-agent-target="rail:strategy" title={strategyBlocked && rightDock !== "strategy" ? strategyBlocked : undefined} onClick={() => { if (rightDock === "strategy") toggle("strategy"); else if (!access && strategyBlocked) setBlocked(strategyBlocked); else if (allowed("strategy-management")) toggle("strategy"); }}><ListTree /><span>策略</span></button>
      <button type="button" className={rightDock === "reminders" ? "active" : ""} aria-label={rightDock === "reminders" ? "收起提醒面板" : "打开提醒面板"} aria-expanded={rightDock === "reminders"} data-agent-target="rail:reminders" onClick={() => { if (rightDock === "reminders" || allowed("preferences")) toggle("reminders"); }}><Bell /><span>提醒</span></button>
      <i className="fdw-rail-sep" aria-hidden="true" />
      <button type="button" className={rightDock === "order" ? "active" : ""} aria-label={rightDock === "order" ? "收起模拟下单" : "打开模拟下单"} data-agent-target="rail:order" onClick={() => { if (rightDock === "order") toggle("order"); else if (allowed("order-proposal")) { onTrade(); toggle("order"); } }}><ReceiptText /><span>下单</span></button>
      <button type="button" className={rightDock === "positions" ? "active" : ""} aria-label={rightDock === "positions" ? "收起持仓面板" : "打开持仓面板"} aria-expanded={rightDock === "positions"} data-agent-target="rail:positions" onClick={() => { if (rightDock === "positions" || allowed("account")) toggle("positions"); }}><BriefcaseBusiness /><span>持仓</span></button>
      {web && <a className="fdw-rail-bottom" data-testid="download-client" data-agent-target="rail:download" aria-label="下载客户端" title="下载客户端" href="https://qilinshuzhi.com/trader/download" target="_blank" rel="noopener noreferrer"><Download /><span>下载</span></a>}
      <button className={web ? undefined : "fdw-rail-bottom"} type="button" title="设置" aria-label="设置" aria-haspopup="dialog" aria-expanded={settingsOpen} data-agent-target="rail:settings" onClick={onSettings}><Settings2 /><span>设置</span></button>
      {blocked && <span role="status">{blocked}</span>}
    </nav>
  );
}
