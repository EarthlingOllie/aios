/** The tab to show: the one asked for (in the URL) if it exists, else the first. */
export function activeTab(ids: string[], requested: string | null): string | undefined {
  return requested !== null && ids.includes(requested) ? requested : ids[0];
}

/**
 * The tab a key press in the tab list moves to, as the ARIA tabs pattern has it: the arrows
 * step (wrapping at the ends), Home and End jump. Null for any other key.
 */
export function keyTarget(key: string, index: number, count: number): number | null {
  switch (key) {
    case "ArrowRight":
      return (index + 1) % count;
    case "ArrowLeft":
      return (index - 1 + count) % count;
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}

/**
 * The URL of tab `id` on the page at `base`: `base` itself for the first tab, `base/<id>` for the
 * others, with `search` (the list's search, say) kept.
 */
export function tabHref(base: string, ids: string[], id: string, search: string): string {
  return (id === ids[0] ? base : `${base}/${id}`) + search;
}

/**
 * Where to send a tab URL that isn't the canonical one, or null if it is: the first tab's own
 * segment and an unknown one go to `base`, and an old `?tab=<id>` link to the tab's path.
 */
export function tabRedirect(
  base: string,
  ids: string[],
  requested: string | undefined,
  search: string,
): string | null {
  const query = new URLSearchParams(search);
  const legacy = query.get("tab");
  if (legacy !== null) {
    query.delete("tab");
    const rest = query.toString();
    return tabHref(base, ids, activeTab(ids, legacy)!, rest ? `?${rest}` : "");
  }
  if (requested === undefined || (requested !== ids[0] && ids.includes(requested))) return null;
  return tabHref(base, ids, ids[0], search);
}
