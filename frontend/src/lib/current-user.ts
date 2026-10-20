import { createContext, useContext } from "react";

import type { Me } from "@/lib/api";

/** The signed-in user, for the pages under the shell (null outside it, e.g. in isolation). */
export const CurrentUserContext = createContext<Me | null>(null);

export function useCurrentUser(): Me | null {
  return useContext(CurrentUserContext);
}

/** Reads the signed-in user again (`/api/me`), for a page that changed what the shell shows of
 * them, such as renaming their organisation. */
export const ReloadCurrentUserContext = createContext<() => void>(() => {});

export function useReloadCurrentUser(): () => void {
  return useContext(ReloadCurrentUserContext);
}
