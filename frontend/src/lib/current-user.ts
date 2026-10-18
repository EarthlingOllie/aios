import { createContext, useContext } from "react";

import type { User } from "@/lib/api";

/** The signed-in user, for the pages under the shell (null outside it, e.g. in isolation). */
export const CurrentUserContext = createContext<User | null>(null);

export function useCurrentUser(): User | null {
  return useContext(CurrentUserContext);
}
