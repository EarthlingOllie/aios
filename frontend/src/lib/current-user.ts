import { createContext, useContext } from "react";

import type { Me, Membership, Role } from "@/lib/api";
import { refusal, type Action } from "@/lib/permissions";

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

/** The organisation the page is open in (`/o/<slug>/`), as `/api/me` lists it: null outside one
 * (an invitation link's page, opened without it). */
export const CurrentOrganisationContext = createContext<Membership | null>(null);

export function useCurrentOrganisation(): Membership | null {
  return useContext(CurrentOrganisationContext);
}

/** The signed-in member's role in the organisation the page is open in (undefined outside one). */
export function useRole(): Role | undefined {
  return useCurrentOrganisation()?.role;
}

/** Why the signed-in member can't take `action` in the organisation the page is open in, as
 * `refusal` says it, or null if they can: for hiding or disabling a control, and saying why. */
export function useRefusal(action: Action): string | null {
  return refusal(useRole(), action);
}
