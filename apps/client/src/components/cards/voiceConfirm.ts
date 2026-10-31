import { useEffect, useState } from "react";

/**
 * Seconds left in the real-time call's spoken confirmation window (`voiceConfirmBy` on a proposal
 * card), ticking once a second; undefined when the card has no open window. The Runtime abandons
 * the card itself when the window closes — this is display only.
 */
export function useVoiceConfirmCountdown(confirmBy: string | null | undefined, active: boolean): number | undefined {
  const deadline = active && confirmBy ? Date.parse(confirmBy) : Number.NaN;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (Number.isNaN(deadline)) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [deadline]);
  if (Number.isNaN(deadline)) return undefined;
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}

export function voiceConfirmLabel(secondsLeft: number | undefined): string {
  return secondsLeft === undefined ? "" : secondsLeft > 0 ? `语音确认 ${secondsLeft} s` : "语音确认已超时";
}
