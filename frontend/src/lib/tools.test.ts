import { describe, expect, it } from "vitest";

import { workflowOptions } from "@/lib/tools";

const live = [
  { id: 1, name: "Summarise" },
  { id: 2, name: "Report" },
];
const archived = [{ id: 3, name: "Old" }];

describe("workflowOptions", () => {
  it("lists the workflows that can be picked, those picked first", () => {
    expect(workflowOptions(live, archived, [2])).toEqual([
      { id: 2, name: "Report", checked: true, note: null },
      { id: 1, name: "Summarise", checked: false, note: null },
    ]);
  });

  it("keeps picked workflows since archived or deleted, saying so, so they can be removed", () => {
    expect(workflowOptions(live, archived, [3, 9, 1])).toEqual([
      { id: 1, name: "Summarise", checked: true, note: null },
      { id: 3, name: "Old", checked: true, note: "archived" },
      { id: 9, name: "Workflow 9", checked: true, note: "deleted" },
      { id: 2, name: "Report", checked: false, note: null },
    ]);
  });

  it("doesn't offer archived workflows that aren't picked", () => {
    expect(workflowOptions(live, archived, []).map((o) => o.id)).toEqual([1, 2]);
  });
});
