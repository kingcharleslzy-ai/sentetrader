import type { AiAvatar as AiAvatarKind } from "./futures/support";
import "./AiAvatar.css";

/** What the figure shows: the orb's call/activity look, plus error and offline. */
export type AiAvatarState = "standby" | "busy" | "listening" | "speaking" | "error" | "offline";

// 像素小牛 (12 × 11): H horn, F face, M muzzle, N nostril, E eye, W eye light.
const BULL = [
  "H..........H",
  "HH........HH",
  ".HHFFFFFFHH.",
  "..FFFFFFFF..",
  ".FFEEFFEEFF.",
  ".FFEWFFEWFF.",
  ".FFFFFFFFFF.",
  "..FMMMMMMF..",
  "..FMNMMNMF..",
  "..FMMMMMMF..",
  "...FFFFFF...",
];
const BULL_PIXELS = BULL.flatMap((row, y) => row.split("").flatMap((cell, x) => cell === "." ? [] : [{ x, y, cell }]));

/** 弈石 / 先手环 / 声纹环 (owner 2026-11-07, from the brand mark): one SVG on a 64 × 64 box. */
function BrandFigure({ kind }: { kind: "stone" | "ring" | "voice" }) {
  return <svg className="ai-avatar-svg" viewBox="0 0 64 64">
    {kind === "stone" ? <>
      <circle className="a-halo" cx="32" cy="32" r="30" />
      <circle className="a-ring" cx="32" cy="32" r="27" />
      <circle className="a-rip" cx="32" cy="32" r="22" />
      <circle className="a-rip a-rip-late" cx="32" cy="32" r="22" />
      <circle className="a-arc" cx="32" cy="32" r="27" />
      <circle className="a-stone" cx="32" cy="32" r="21" />
      <ellipse className="a-hl" cx="25.5" cy="24.5" rx="7" ry="4.2" />
    </> : kind === "ring" ? <>
      <circle className="b-glow" cx="32" cy="32" r="30" />
      <circle className="b-disc" cx="32" cy="32" r="25" />
      <circle className="b-rip" cx="32" cy="32" r="13" />
      <circle className="b-ring" cx="32" cy="32" r="13" />
      <circle className="b-dot" cx="32" cy="32" r="5" />
    </> : <>
      <circle className="e-disc" cx="32" cy="32" r="26" />
      <circle className="e-ring" cx="32" cy="32" r="26" />
      {/* Heights 8 / 14 / 20 / 14 / 8; data-step = distance from the centre bar. */}
      {[[16, 28, 8], [23, 25, 14], [30, 22, 20], [37, 25, 14], [44, 28, 8]].map(([x, y, height], index) =>
        <rect key={x} className="e-bar" data-step={Math.abs(index - 2)} x={x} y={y} width="4" height={height} rx="2" />)}
    </>}
  </svg>;
}

/**
 * The AI's figure at `size` px: 弈石, 先手环, 声纹环, 灵动双眼 or 像素小牛 (设置 → 界面 → AI 形象).
 * An error looks like offline (grey; the reason is text in the orb's menu). Purely decorative; the
 * owning control carries the label.
 */
export function AiAvatar({ kind, state, size = 64, level = 0 }: { kind: AiAvatarKind; state: AiAvatarState; size?: number; level?: number }) {
  const look = state === "error" ? "offline" : state;
  return (
    <span className="ai-avatar" data-kind={kind} data-state={look} aria-hidden="true"
      style={{ width: size, height: size, "--avatar-scale": size / 64, "--voice-level": level } as React.CSSProperties}>
      {kind === "stone" || kind === "ring" || kind === "voice" ? <BrandFigure kind={kind} /> : <span className="ai-avatar-stage">
        {kind === "eyes" ? <span className="ai-avatar-tile eyes">
          <span className="eyes-pair"><i /><i /></span>
          <span className="eyes-dots"><i /><i /><i /></span>
          <i className="eyes-mouth" />
        </span> : <span className="ai-avatar-pixel">
          <svg viewBox="0 0 12 11" shapeRendering="crispEdges">
            {BULL_PIXELS.map(({ x, y, cell }) => <rect key={`${x}-${y}`} x={x} y={y} width="1" height="1" className={`px-${cell}${(cell === "M" && (y === 7 || y === 9)) ? " px-mouth" : ""}`} />)}
          </svg>
          <span className="pixel-dots"><i /><i /><i /></span>
          <span className="pixel-waves"><i /><i /></span>
        </span>}
        <span className="ai-avatar-ripple" />
      </span>}
    </span>
  );
}
