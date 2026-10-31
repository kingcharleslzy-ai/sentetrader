import { createContext, type MutableRefObject } from "react";
export type StrategyNavigation = { selectedStrategyId?: string; tab?: "library" | "fees"; designerOpen?: boolean };
export type StrategyNavigationState = {
  selectedStrategyId: string; tab: "library" | "fees"; designerOpen: boolean;
  items: { strategyId: string; name: string; version: number }[];
};
export const UiNavigationContext = createContext<{
  publish: (state: StrategyNavigationState | null) => void;
  handler: MutableRefObject<((changes: StrategyNavigation) => void) | undefined>;
} | null>(null);
