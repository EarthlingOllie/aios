import { describe, expect, it } from "vitest";

import type { ImportedItem, ImportResult } from "@/lib/api";
import { importLines, importedPath } from "@/lib/imports";

function item(changes: Partial<ImportedItem>): ImportedItem {
  return {
    kind: "agent",
    ref: "agent-1",
    name: "Bot",
    imported_name: "Bot",
    renamed: false,
    unrunnable: null,
    id: null,
    ...changes,
  };
}

describe("importLines", () => {
  it("says what is made, what is renamed and what can't run", () => {
    const skill = item({
      kind: "skill",
      ref: "skill-1",
      name: "Haiku",
      imported_name: "Haiku",
    });
    const agent = item({
      imported_name: "Bot (imported)",
      renamed: true,
      unrunnable: "This version can't run: Unknown tool: crm",
    });
    expect(importLines([skill, agent])).toEqual([
      { key: "skill-1", text: "Skill Haiku", note: null, warning: null },
      {
        key: "agent-1",
        text: "Agent Bot (imported)",
        note: "renamed from Bot: that name is taken",
        warning: "This version can't run: Unknown tool: crm",
      },
    ]);
  });
});

describe("importedPath", () => {
  it("is the made agent's or workflow's page", () => {
    const result = (root: ImportedItem): ImportResult => ({
      dry_run: false,
      root,
      items: [root],
    });
    expect(importedPath(result(item({ id: 7 })))).toBe("/agents/7");
    expect(importedPath(result(item({ kind: "workflow", id: 3 })))).toBe(
      "/workflows/3",
    );
  });

  it("is nothing for a dry run", () => {
    expect(
      importedPath({ dry_run: true, root: item({}), items: [] }),
    ).toBeNull();
  });
});
