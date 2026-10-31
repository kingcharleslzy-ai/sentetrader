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

/**
 * The AI's figure, drawn on a 64 px stage and scaled to `size`: K 线音柱, 灵动双眼 or 像素小牛
 * (设置 → 界面 → AI 形象). Purely decorative; the owning control carries the label.
 */
export function AiAvatar({ kind, state, size = 64, level = 0 }: { kind: AiAvatarKind; state: AiAvatarState; size?: number; level?: number }) {
  return (
    <span className="ai-avatar" data-kind={kind} data-state={state} aria-hidden="true"
      style={{ width: size, height: size, "--avatar-scale": size / 64, "--voice-level": level } as React.CSSProperties}>
      <span className="ai-avatar-stage">
        {kind === "kline" ? <span className="ai-avatar-tile kline">
          <i className="kline-shine" />
          {[0, 1, 2].map(index => <span key={index} className="kline-candle" data-index={index}><i className="kline-wick" /><i className="kline-body" /></span>)}
        </span> : kind === "eyes" ? <span className="ai-avatar-tile eyes">
          <span className="eyes-pair"><i /><i /></span>
          <span className="eyes-dots"><i /><i /><i /></span>
          <i className="eyes-mouth" />
          <span className="eyes-cross">××</span>
        </span> : <span className="ai-avatar-pixel">
          <svg viewBox="0 0 12 11" shapeRendering="crispEdges">
            {BULL_PIXELS.map(({ x, y, cell }) => <rect key={`${x}-${y}`} x={x} y={y} width="1" height="1" className={`px-${cell}${(cell === "M" && (y === 7 || y === 9)) ? " px-mouth" : ""}`} />)}
          </svg>
          <span className="pixel-dots"><i /><i /><i /></span>
          <span className="pixel-waves"><i /><i /></span>
          <b className="pixel-alert">!</b>
        </span>}
        <span className="ai-avatar-ripple" />
      </span>
    </span>
  );
}
