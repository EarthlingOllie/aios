import { createContext, useContext } from "react";

import type { Me } from "@/lib/api";

/** The signed-in user, for the pages under the shell (null outside it, e.g. in isolation). */
export const CurrentUserContext = createContext<Me | null>(null);

export function useCurrentUser(): Me | null {
  return useContext(CurrentUserContext);
}

/**
 * Replaces the signed-in user everywhere they're shown (the sidebar, and whatever reads
 * `useCurrentUser`), e.g. with what a profile edit answered. A no-op outside the shell.
 */
export const SetCurrentUserContext = createContext<(user: Me) => void>(() => {});

export function useSetCurrentUser(): (user: Me) => void {
  return useContext(SetCurrentUserContext);
}

/** Reads the signed-in user again (`/api/me`), for a page that changed what the shell shows of
 * them, such as renaming their organisation. */
export const ReloadCurrentUserContext = createContext<() => void>(() => {});

export function useReloadCurrentUser(): () => void {
  return useContext(ReloadCurrentUserContext);
}
