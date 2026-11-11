import type { Membership } from "@/lib/api";

/**
 * Organisations in links: every signed-in page is under `/o/<slug>/`, the organisation's slug
 * (the API names it by its id). The router's basename is the prefix (see `main.tsx`), so the
 * app's own links (`/agents`) stay in the organisation the page was opened in, and two tabs can
 * hold two organisations. A path without one (an old bookmark, the root) opens the same page in
 * the organisation used most recently, remembered in the browser, else the one joined most
 * recently; a person in none is sent to the welcome page (`WELCOME`).
 */

/** The welcome page, outside any organisation: it creates one, or explains invitation links. */
export const WELCOME = "/welcome";

const PREFIX = /^\/o\/([^/?#]+)(?=$|[/?#])/;

/** The organisation a path names (`/o/acme/agents` names `acme`), or null if it names none. */
export function organisationInPath(pathname: string): string | null {
  const match = PREFIX.exec(pathname);
  return match ? decodeURIComponent(match[1]) : null;
}

/** The prefix a page in the organisation has: what the router's basename is in it. */
export function organisationBase(slug: string): string {
  return `/o/${encodeURIComponent(slug)}`;
}

/** `path` (with any query and hash) in the organisation, unless it names one already. */
export function inOrganisation(slug: string, path: string): string {
  if (organisationInPath(path) !== null) return path;
  return organisationBase(slug) + path;
}

/** Open `path` in another organisation (or this one afresh): a new page load, since the
 * organisation is the router's basename. */
export function openInOrganisation(slug: string, path: string): void {
  window.location.assign(inOrganisation(slug, path));
}

/** Which organisation a path without one opens in: the one used most recently (`recent`, most
 * recent first) that the user is still in, else the one they joined most recently (listed
 * first); null if in none. */
export function organisationToOpen(memberships: Membership[], recent: string[]): string | null {
  const used = recent.map((slug) => memberships.find((m) => m.slug === slug)).find((m) => m !== undefined);
  return (used ?? memberships[0])?.slug ?? null;
}

/** Where to land with none named (after leaving one, say): the organisation `organisationToOpen`
 * picks, or the welcome page if the user is in none. */
export function landingPath(memberships: Membership[], recent: string[]): string {
  const slug = organisationToOpen(memberships, recent);
  return slug === null ? WELCOME : `${organisationBase(slug)}/`;
}

const RECENT = "aios.organisations";
// How many it remembers: more than anyone switches between.
const RECENT_MAX = 20;

/** The organisations used in this browser, most recently first, as far as it remembers (and
 * storage is allowed). */
export function recentOrganisations(): string[] {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(RECENT) ?? "[]");
    return Array.isArray(stored) ? stored.filter((slug): slug is string => typeof slug === "string") : [];
  } catch {
    return [];
  }
}

/** Remember the organisation as the one used most recently; nothing happens where storage is
 * refused. */
export function rememberOrganisation(slug: string): void {
  const recent = [slug, ...recentOrganisations().filter((s) => s !== slug)].slice(0, RECENT_MAX);
  try {
    localStorage.setItem(RECENT, JSON.stringify(recent));
  } catch {
    // Private browsing, or storage turned off: links without one open the most recently joined.
  }
}
