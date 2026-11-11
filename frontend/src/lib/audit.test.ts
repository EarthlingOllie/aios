import { describe, expect, it } from "vitest";

import { auditQuery, type AuditEntry } from "@/lib/api";
import { actorName, detailsText, targetText } from "@/lib/audit";

function entry(fields: Partial<AuditEntry>): AuditEntry {
  return {
    id: 1,
    at: "2026-11-12T09:00:00Z",
    actor: { kind: "person", user: { id: 3, name: "Ada Admin" }, agent: null, run_id: null },
    action: "agent.archived",
    target_kind: "agent",
    target_id: "7",
    details: {},
    ...fields,
  };
}

describe("actorName", () => {
  it("names the person, the agent in its run, or the platform", () => {
    expect(actorName(entry({}))).toBe("Ada Admin");
    expect(
      actorName(entry({ actor: { kind: "agent", user: null, agent: { id: 4, name: "Scout" }, run_id: 9 } })),
    ).toBe("Scout (run #9)");
    expect(actorName(entry({ actor: { kind: "system", user: null, agent: null, run_id: null } }))).toBe("The platform");
    expect(actorName(entry({ actor: { kind: "person", user: null, agent: null, run_id: null } }))).toBe(
      "A deleted user",
    );
  });
});

describe("targetText", () => {
  it("names what it was done to by its name where it has one", () => {
    expect(targetText(entry({ details: { name: "Scout" } }))).toBe("agent Scout");
    expect(targetText(entry({ target_kind: "table", target_id: "data.fish" }))).toBe("table data.fish");
    expect(targetText(entry({ target_kind: null, target_id: null }))).toBe("");
  });
});

describe("detailsText", () => {
  it("shows a change from one value to another, and the rest as key: value", () => {
    expect(detailsText(entry({ details: { from: "editor", to: "admin" } }))).toBe("editor → admin");
    expect(detailsText(entry({ details: { table: "data.fish", row_count: 2 } }))).toBe(
      "table: data.fish, row_count: 2",
    );
    expect(detailsText(entry({ details: { name: "Scout" } }))).toBe("");
  });
});

describe("auditQuery", () => {
  it("sends only the filters given, and the cursor", () => {
    expect(auditQuery({}).toString()).toBe("");
    expect(auditQuery({ actor: 3, action: " agent ", since: "2026-11-01", until: "2026-11-12" }, "c1").toString()).toBe(
      "actor=3&action=agent&since=2026-11-01&until=2026-11-12&before=c1",
    );
  });
});
