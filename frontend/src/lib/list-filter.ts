/** Searching the lists on the left of each section (`MasterDetail`). */

/** Lower case, with accents stripped, so "Café" and "cafe" compare equal. */
function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

/**
 * Whether an item with these fields (its name, description, …) matches the search: every word
 * of the query has to appear in at least one field, ignoring case and accents. An empty query
 * matches everything.
 */
export function matchesQuery(fields: string[], query: string): boolean {
  const words = fold(query).split(/\s+/).filter(Boolean);
  const folded = fields.map(fold);
  return words.every((word) => folded.some((field) => field.includes(word)));
}

/** What the lists search by default: an item's name and description. */
export function nameAndDescription(item: { name: string; description: string }): string[] {
  return [item.name, item.description];
}

/** The URL parameters that belong to a list (its search), and so carry over between its items. */
export const LIST_PARAMS = ["q"];

/**
 * `path` with the list's own parameters from `params` added, so a filtered list stays filtered
 * as you open its items. Others, such as the open `tab`, are left behind: another item opens
 * on its first tab.
 */
export function listHref(path: string, params: URLSearchParams): string {
  const kept = new URLSearchParams();
  for (const key of LIST_PARAMS) {
    const value = params.get(key);
    if (value) kept.set(key, value);
  }
  const query = kept.toString();
  return query ? `${path}?${query}` : path;
}
