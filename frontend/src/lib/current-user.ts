import { createContext, useContext } from "react";

import type { User } from "@/lib/api";

/** The signed-in user, for the pages under the shell (null outside it, e.g. in isolation). */
export const CurrentUserContext = createContext<User | null>(null);

export function useCurrentUser(): User | null {
  return useContext(CurrentUserContext);
}

/**
 * Replaces the signed-in user everywhere they're shown (the sidebar, and whatever reads
 * `useCurrentUser`), e.g. with what a profile edit answered. A no-op outside the shell.
 */
export const SetCurrentUserContext = createContext<(user: User) => void>(() => {});

export function useSetCurrentUser(): (user: User) => void {
  return useContext(SetCurrentUserContext);
}
