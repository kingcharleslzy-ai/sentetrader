/** The account the attached session acts on — the sole source is the server's
 *  runtime.status (never a local guess). Requests without their own session
 *  route their account reads through the X-AI-Trader-Account header that names it;
 *  figures worked out for the previous account listen for the switch. */
let activeTradingAccountId: string | null = null;
const listeners = new Set<() => void>();

export function activeTradingAccount() { return activeTradingAccountId; }
export function setActiveTradingAccount(id: string | null) {
  if (id === activeTradingAccountId) return;
  activeTradingAccountId = id;
  listeners.forEach(listener => listener());
}
export function onTradingAccountChange(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
