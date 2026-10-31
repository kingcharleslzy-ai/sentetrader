/**
 * 会话账户切换次序 (#932 follow-up): the one place a window decides whether a runtime.status is accepted, and the
 * revision its next account switch names. The Runtime keeps the session's account revision on the session row (it only
 * grows); with two Runtime slots a status from before a newer switch can arrive late, and taking it would show — and
 * act on — the account that switch replaced. So a status whose `accountGeneration` is lower than the highest this
 * window has seen for the session (from statuses and from switch answers) is dropped whole: no account change, no
 * header update. A status without the field (a Runtime before it) is accepted as before. Keyed by session id (a UUID,
 * unique across Runtimes).
 */
const known = new Map<string, number>();

/** The highest revision seen for the session; undefined until a Runtime that reports it answered. */
export function knownAccountRevision(sessionId: string): number | undefined {
  return known.get(sessionId);
}

/** A revision the Runtime reported for the session (a status or a switch answer): kept if higher. */
export function noteAccountRevision(sessionId: string, revision: number): void {
  if ((known.get(sessionId) ?? -1) < revision) known.set(sessionId, revision);
}

/** Whether the Runtime's account state at `revision` is taken for the session (and the revision noted): never one
 *  older than what this window already took — a runtime.status, or the account a 409 reports. */
export function admitAccountRevision(sessionId: string, revision: number): boolean {
  const seen = known.get(sessionId);
  if (seen !== undefined && revision < seen) return false;
  noteAccountRevision(sessionId, revision);
  return true;
}

/** Whether a runtime.status for the session is taken (and its revision noted); false = an older one arriving late. */
export function admitRuntimeStatus(sessionId: string, status: { accountGeneration?: number } | null | undefined): boolean {
  const revision = status?.accountGeneration;
  return typeof revision !== "number" || admitAccountRevision(sessionId, revision);
}

/** Tests only: forget every session's revision. */
export function resetAccountRevisions(): void {
  known.clear();
}
