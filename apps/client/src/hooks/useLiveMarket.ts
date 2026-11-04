import { createContext, useContext } from "react";
import { sameContract, type MarketQuoteSnapshot, type MarketWorkbenchSnapshot } from "../api";
import { useFeedMarket } from "../runtimeFeed";

/** Whether the session's push is what the window's market comes from (not a replay, not a poll): set by the app. */
export const LiveMarketContext = createContext(false);

/**
 * 电脑版流畅度 ② (2026-11-04): the window renders from a market that takes price-only patches at most once a second
 * (runtimeFeed `settleMarket`); the chart, the price and the ticket call this to show every patch. It is the pushed market
 * while the push covers this contract and period, else the one given (keeping the window's display name for the contract).
 */
export function useLiveMarket<T extends MarketWorkbenchSnapshot>(market: T | undefined): T | undefined {
  const live = useContext(LiveMarketContext);
  const pushed = useFeedMarket();
  if (!live || !market || !pushed || pushed === market || !sameContract(pushed.quote.symbol, market.quote.symbol)
      || pushed.durationSeconds !== market.durationSeconds) return market;
  return { ...market, ...pushed, quote: { ...pushed.quote, name: market.quote.name } };
}

/** The same for a quote alone (the phone's header). */
export function useLiveQuote(quote: MarketQuoteSnapshot | undefined): MarketQuoteSnapshot | undefined {
  const live = useContext(LiveMarketContext);
  const pushed = useFeedMarket();
  if (!live || !quote || !pushed || pushed.quote === quote || !sameContract(pushed.quote.symbol, quote.symbol)) return quote;
  return { ...pushed.quote, name: quote.name };
}
