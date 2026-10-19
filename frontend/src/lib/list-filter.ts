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

/** One choice of a filter group: the items it keeps. */
export type ListFilterOption<T> = { key: string; label: string; test: (item: T) => boolean };

/**
 * A set of filters, of which one (or none: `all`, the default) is chosen at a time, kept in the
 * URL as `?<param>=<key>`. Shown as a row of buttons, or as a select. A list can have several
 * groups, each with its own parameter, and an item has to pass all of them.
 */
export type ListFilterGroup<T> = {
  param: string;
  /** Names the group for screen readers, e.g. "Show". */
  label: string;
  /** The default's label: "All", or "Any skill" for a select. */
  all: string;
  kind?: "buttons" | "select";
  options: ListFilterOption<T>[];
};

/**
 * A way to order a list, kept in the URL as `?sort=<key>`. The first is the default and is left
 * out of the URL. One without `compare` keeps the server's order (the lists come sorted by name).
 */
export type ListSort<T> = { key: string; label: string; compare?: (a: T, b: T) => number };

/** The option `key` names, if there is one; an unknown key is as if none were chosen. */
export function chosenOption<O extends { key: string }>(options: O[], key: string | null): O | undefined {
  return key === null ? undefined : options.find((option) => option.key === key);
}

/** Most recently updated first. */
export function byRecentlyUpdated(a: { updated_at: string }, b: { updated_at: string }): number {
  return Date.parse(b.updated_at) - Date.parse(a.updated_at);
}

/** Most runs first. */
export function byMostRuns(a: { run_count: number }, b: { run_count: number }): number {
  return b.run_count - a.run_count;
}

/** The sorts the lists share: by name, the server's order and so the default, and by updated. */
export const NAME_SORT: ListSort<unknown> = { key: "name", label: "Name" };
export const UPDATED_SORT: ListSort<{ updated_at: string }> = {
  key: "updated",
  label: "Recently updated",
  compare: byRecentlyUpdated,
};

/**
 * The items a list shows: those matching the search (`query`, over the `search` fields) and
 * the filter chosen in each group (from `params`), in the chosen sort's order. A sort keeps
 * ties in the order they came in. `items` itself is left as it was.
 */
export function narrowList<T>(
  items: T[],
  params: URLSearchParams,
  {
    query,
    search,
    filters = [],
    sorts = [],
  }: {
    query: string;
    search?: (item: T) => string[];
    filters?: ListFilterGroup<T>[];
    sorts?: ListSort<T>[];
  },
): T[] {
  const tests = filters.flatMap(
    (group) => chosenOption(group.options, params.get(group.param))?.test ?? [],
  );
  if (search && query) tests.push((item) => matchesQuery(search(item), query));
  const shown = tests.length ? items.filter((item) => tests.every((test) => test(item))) : items;
  const compare = chosenOption(sorts, params.get("sort"))?.compare;
  return compare ? [...shown].sort(compare) : shown;
}

/**
 * The URL parameters that belong to a list (its search, filters and sort), and so carry over
 * between its items. A list's own filter groups add theirs here.
 */
export const LIST_PARAMS = ["q", "filter", "sort", "skill", "tool"];

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
