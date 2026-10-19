import { describe, expect, it } from "vitest";

import {
  byMostRuns,
  byRecentlyUpdated,
  listHref,
  matchesQuery,
  narrowList,
  type ListFilterGroup,
  type ListSort,
} from "@/lib/list-filter";

describe("matchesQuery", () => {
  it("matches everything when the query is empty or blank", () => {
    expect(matchesQuery(["Researcher"], "")).toBe(true);
    expect(matchesQuery(["Researcher"], "   ")).toBe(true);
  });

  it("matches a word anywhere in a field", () => {
    expect(matchesQuery(["Researcher", "Finds sources"], "search")).toBe(true);
    expect(matchesQuery(["Researcher", "Finds sources"], "writer")).toBe(false);
  });

  it("needs every word to match, in any order and any field", () => {
    expect(matchesQuery(["Researcher", "Finds sources"], "sources research")).toBe(true);
    expect(matchesQuery(["Researcher", "Finds sources"], "sources writer")).toBe(false);
  });

  it("ignores case", () => {
    expect(matchesQuery(["Researcher"], "RESEARCH")).toBe(true);
  });

  it("ignores accents, either way round", () => {
    expect(matchesQuery(["Café notes"], "cafe")).toBe(true);
    expect(matchesQuery(["Cafe notes"], "café")).toBe(true);
  });

  it("ignores a mark that lower-casing adds", () => {
    // "İ" lower-cases to "i" plus a combining dot above.
    expect(matchesQuery(["İstanbul office"], "istanbul")).toBe(true);
  });

  it("doesn't match a word split across two fields", () => {
    expect(matchesQuery(["Re", "search"], "research")).toBe(false);
  });
});

describe("listHref", () => {
  it("is the bare path when the list has no parameters", () => {
    expect(listHref("/agents", new URLSearchParams())).toBe("/agents");
  });

  it("carries the search", () => {
    expect(listHref("/agents/3", new URLSearchParams("q=web search"))).toBe(
      "/agents/3?q=web+search",
    );
  });

  it("leaves out parameters that aren't the list's, such as the tab", () => {
    expect(listHref("/agents/3", new URLSearchParams("q=web&tab=runs"))).toBe("/agents/3?q=web");
    expect(listHref("/agents/3", new URLSearchParams("tab=runs"))).toBe("/agents/3");
  });

  it("carries the filter and sort as well", () => {
    expect(listHref("/agents", new URLSearchParams("filter=used&sort=runs&tab=runs"))).toBe(
      "/agents?filter=used&sort=runs",
    );
  });

  it("keeps the path's own query, such as the tab it opens, ahead of the list's", () => {
    expect(listHref("/workflows/3?tab=runs", new URLSearchParams("q=web&tab=graph"))).toBe(
      "/workflows/3?tab=runs&q=web",
    );
    expect(listHref("/workflows/3?tab=runs", new URLSearchParams())).toBe("/workflows/3?tab=runs");
  });
});

type Row = { id: number; name: string; description: string; run_count: number; updated_at: string };
const row = (id: number, name: string, run_count: number, updated_at: string): Row => ({
  id,
  name,
  description: "",
  run_count,
  updated_at,
});
// In the server's order, by name.
const rows = [
  row(1, "Alpha", 0, "2026-01-03T00:00:00Z"),
  row(2, "Beta", 5, "2026-01-01T00:00:00Z"),
  row(3, "Gamma", 2, "2026-01-02T00:00:00Z"),
];
const ids = (items: Row[]) => items.map((item) => item.id);

const used: ListFilterGroup<Row> = {
  param: "filter",
  label: "Show",
  all: "All",
  options: [
    { key: "used", label: "Used", test: (r) => r.run_count > 0 },
    { key: "unused", label: "Unused", test: (r) => r.run_count === 0 },
  ],
};
const recent: ListFilterGroup<Row> = {
  param: "since",
  label: "Updated",
  all: "Any time",
  kind: "select",
  options: [{ key: "2026", label: "Since 2 January", test: (r) => r.updated_at >= "2026-01-02" }],
};
const sorts: ListSort<Row>[] = [
  { key: "name", label: "Name" },
  { key: "updated", label: "Recently updated", compare: byRecentlyUpdated },
  { key: "runs", label: "Most runs", compare: byMostRuns },
];
const narrow = (query: string, params: string) =>
  ids(
    narrowList(rows, new URLSearchParams(params), {
      query,
      search: (r) => [r.name],
      filters: [used, recent],
      sorts,
    }),
  );

describe("narrowList", () => {
  it("keeps every item, in the server's order, with nothing chosen", () => {
    expect(narrow("", "")).toEqual([1, 2, 3]);
  });

  it("keeps the items the chosen filter allows", () => {
    expect(narrow("", "filter=used")).toEqual([2, 3]);
    expect(narrow("", "filter=unused")).toEqual([1]);
  });

  it("combines filter groups, and the search, so an item has to pass all of them", () => {
    expect(narrow("", "filter=used&since=2026")).toEqual([3]);
    expect(narrow("", "filter=unused&since=2026")).toEqual([1]);
    expect(narrow("gam", "filter=used&since=2026")).toEqual([3]);
    expect(narrow("beta", "filter=used&since=2026")).toEqual([]);
  });

  it("ignores a filter or sort it doesn't know, as if none were chosen", () => {
    expect(narrow("", "filter=zebra")).toEqual([1, 2, 3]);
    expect(narrow("", "sort=zebra")).toEqual([1, 2, 3]);
  });

  it("sorts by most recently updated first", () => {
    expect(narrow("", "sort=updated")).toEqual([1, 3, 2]);
  });

  it("sorts by most runs first", () => {
    expect(narrow("", "sort=runs")).toEqual([2, 3, 1]);
  });

  it("keeps the server's order for a sort with no comparison, and among ties", () => {
    expect(narrow("", "sort=name")).toEqual([1, 2, 3]);
    const tied = [row(1, "B", 1, "2026-01-01"), row(2, "A", 1, "2026-01-01")];
    expect(ids(narrowList(tied, new URLSearchParams("sort=runs"), { query: "", sorts }))).toEqual([1, 2]);
  });

  it("sorts what's left after filtering", () => {
    expect(narrow("", "filter=used&sort=updated")).toEqual([3, 2]);
  });

  it("doesn't change the list it was given", () => {
    narrowList(rows, new URLSearchParams("sort=runs"), { query: "", sorts });
    expect(ids(rows)).toEqual([1, 2, 3]);
  });
});
