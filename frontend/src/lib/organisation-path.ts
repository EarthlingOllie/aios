import type { Membership } from "@/lib/api";

/**
 * Organisations in links: every signed-in page is under `/o/<slug>/`, the organisation's slug
 * (the API names it by its id). The router's basename is the prefix (see `main.tsx`), so the
 * app's own links (`/agents`) stay in the organisation the page was opened in, and two tabs can
 * hold two organisations. A path without one (an old bookmark, the root) opens the same page in
 * the organisation used last, remembered in the browser, else the one joined most recently.
 */

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

/** Which organisation a path without one opens in: the one used last (`remembered`) if the user
 * is still in it, else the one they joined most recently (listed first); null if in none. */
export function organisationToOpen(memberships: Membership[], remembered: string | null): string | null {
  const last = memberships.find((m) => m.slug === remembered);
  return (last ?? memberships[0])?.slug ?? null;
}

const LAST = "aios.organisation";

/** The organisation used last in this browser, if any is remembered (and storage is allowed). */
export function lastOrganisation(): string | null {
  try {
    return localStorage.getItem(LAST);
  } catch {
    return null;
  }
}

/** Remember the organisation as the one used last; nothing happens where storage is refused. */
export function rememberOrganisation(slug: string): void {
  try {
    localStorage.setItem(LAST, slug);
  } catch {
    // Private browsing, or storage turned off: links without one open the most recently joined.
  }
}
