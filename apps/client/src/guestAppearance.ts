import { createContext } from "react";
import type { DurationSeconds } from "./userPreferences";
import type { ThemePreference } from "./theme";
/** Carry the guest's current view across login; it is already a user selection, not an account default.
 *  It applies to the first member workspace only: `consume` clears it once that workspace has read it, so
 *  a later remount (entering / leaving 回放) keeps the account's own theme and period. */
export interface GuestAppearance {
  theme: ThemePreference; duration: DurationSeconds; consume?: () => void;
  /** The login sheet's result line (e.g. 注册成功，已开好本站模拟账户), shown once the member workspace connects. */
  notice?: string;
}
export const GuestAppearanceContext = createContext<GuestAppearance | undefined>(undefined);
