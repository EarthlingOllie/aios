import { describe, expect, it } from "vitest";

import { activeTab, keyTarget, tabHref, tabRedirect } from "@/lib/tabs";

const ids = ["context", "runs", "versions"];

describe("activeTab", () => {
  it("is the requested tab when there is one", () => {
    expect(activeTab(ids, "runs")).toBe("runs");
  });

  it("is the first tab when none is requested", () => {
    expect(activeTab(ids, null)).toBe("context");
  });

  it("is the first tab when the requested one doesn't exist", () => {
    expect(activeTab(ids, "schedule")).toBe("context");
  });
});

describe("keyTarget", () => {
  it("moves right and left", () => {
    expect(keyTarget("ArrowRight", 0, 3)).toBe(1);
    expect(keyTarget("ArrowLeft", 2, 3)).toBe(1);
  });

  it("wraps around at either end", () => {
    expect(keyTarget("ArrowRight", 2, 3)).toBe(0);
    expect(keyTarget("ArrowLeft", 0, 3)).toBe(2);
  });

  it("jumps to the first and last tab", () => {
    expect(keyTarget("Home", 2, 3)).toBe(0);
    expect(keyTarget("End", 0, 3)).toBe(2);
  });

  it("ignores other keys", () => {
    expect(keyTarget("Enter", 1, 3)).toBeNull();
    expect(keyTarget("ArrowDown", 1, 3)).toBeNull();
  });
});

describe("tabHref", () => {
  it("is the page's own path for the first tab, and a segment under it for the others", () => {
    expect(tabHref("/agents/3", ids, "context", "")).toBe("/agents/3");
    expect(tabHref("/agents/3", ids, "runs", "")).toBe("/agents/3/runs");
  });

  it("keeps the query, such as the list's search", () => {
    expect(tabHref("/agents/3", ids, "runs", "?q=web&sort=runs")).toBe("/agents/3/runs?q=web&sort=runs");
    expect(tabHref("/agents/3", ids, "context", "?q=web")).toBe("/agents/3?q=web");
  });
});

describe("tabRedirect", () => {
  it("is nothing for the page's own path or a known tab's", () => {
    expect(tabRedirect("/agents/3", ids, undefined, "?q=web")).toBeNull();
    expect(tabRedirect("/agents/3", ids, "runs", "")).toBeNull();
  });

  it("sends the first tab's segment, and an unknown one, to the page's own path", () => {
    expect(tabRedirect("/agents/3", ids, "context", "?q=web")).toBe("/agents/3?q=web");
    expect(tabRedirect("/agents/3", ids, "nope", "")).toBe("/agents/3");
  });

  it("turns an old ?tab= link into the tab's path, keeping the rest of the query", () => {
    expect(tabRedirect("/agents/3", ids, undefined, "?tab=runs&q=web")).toBe("/agents/3/runs?q=web");
    expect(tabRedirect("/agents/3", ids, undefined, "?tab=context")).toBe("/agents/3");
    expect(tabRedirect("/data", ["overview", "changes"], undefined, "?tab=changes&request=7")).toBe(
      "/data/changes?request=7",
    );
  });
});
