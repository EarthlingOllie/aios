import { describe, expect, it } from "vitest";

import { listHref, matchesQuery } from "@/lib/list-filter";

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
});
