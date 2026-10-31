import type { ConversationCard } from "../../state/conversation";

/** A proposal card the user confirms with the card's own 确认 (no second dialog, owner 2026-10-19). */
export type ConfirmableCard = Extract<ConversationCard, { type: "trade" | "close-batch" | "spread" | "risk-rule" }>;

export function confirmable(card: ConversationCard): card is ConfirmableCard {
  return card.type === "trade" || card.type === "close-batch" || card.type === "spread" || card.type === "risk-rule";
}

/** The card's own account when it is not the one the conversation is on now — a strategy's card goes to the
 *  instance's account even after the conversation switched (#1011). Decided by account id (the card's
 *  `accountId` against the session's, #1005's `onCurrentAccount`), named from the account directory; undefined
 *  for a card on the current account or without an account. */
export function otherAccountNote(cardAccount: string | undefined, actions: {
  onCurrentAccount?: (accountId?: string) => boolean; accountName?: (accountId: string) => string | undefined;
}, label?: string): string | undefined {
  if (!cardAccount || actions.onCurrentAccount?.(cardAccount) !== false) return undefined;
  return `下到${actions.accountName?.(cardAccount) ?? label ?? cardAccount}（不是当前对话的账户）`;
}

/** Why the card's 确认 cannot be pressed now, shown beside the disabled button and enforced by the confirm
 *  itself; undefined when it can.
 *  `gateway`: why the current account or its simulation gateway cannot take a confirmation (undefined = it can) —
 *  the account and gateway only, never the chart's contract (#932 B1). It describes the account the conversation
 *  is on, so it does not apply to a card for another account (`onCurrentAccount` false, #1011 instance cards):
 *  that account's gateway is checked by the Runtime when the card is confirmed (#932 F1). Everything else (the
 *  card's own quote, tick, price limits, position, approval and version) is re-checked there too. */
export function confirmBlocked(
  card: ConfirmableCard, currentGateway: string | undefined, onCurrentAccount?: (accountId?: string) => boolean,
): string | undefined {
  const cardAccount = "accountId" in card.payload ? card.payload.accountId : undefined;
  const gateway = cardAccount && onCurrentAccount?.(cardAccount) === false ? undefined : currentGateway;
  const unavailable = `模拟下单暂时不可用${gateway ? `（${gateway}）` : ""}，恢复后再确认`;
  if (card.type === "trade") {
    if (gateway !== undefined) return unavailable;  // nothing is recorded as a mere candidate (PR #1004 review)
    if (card.payload.executionEligibleAtCreation !== true) return "这张卡建立时行情不满足下单条件，请重新出卡";
    return undefined;
  }
  if (card.type === "close-batch" || card.type === "spread") return gateway === undefined ? undefined : unavailable;
  return undefined;
}
