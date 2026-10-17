import { describe, expect, it } from "vitest";

import { ApiError, type AgentDependents } from "@/lib/api";
import { hasDependents, parseDeleteRefusal, resolutions, unresolved } from "@/lib/agent-delete";

const none: AgentDependents = { agent_runs: [], workflow_runs: [], workflows: [], triggers: [] };

const trigger = { id: 9, enabled: true, cron: "0 9 * * *", description: "At 09:00", timezone: "UTC", next_run_at: null };

const some: AgentDependents = {
  agent_runs: [
    { id: 1, title: "Chat", waiting: true, running: false, trigger_id: null, updated_at: "2026-10-14T09:00:00Z" },
  ],
  workflow_runs: [
    { id: 2, workflow_id: 5, workflow_name: "Builds", status: "waiting", created_at: "2026-10-14T09:00:00Z" },
  ],
  workflows: [
    { id: 5, name: "Builds", version: 1, direct: true, triggers: [trigger] },
    { id: 6, name: "Outer", version: 2, direct: false, triggers: [] },
  ],
  triggers: [trigger],
};

describe("hasDependents", () => {
  it("is false when nothing would be cut off; its own schedules just go with it", () => {
    expect(hasDependents(none)).toBe(false);
    expect(hasDependents({ ...none, triggers: [trigger] })).toBe(false);
    expect(hasDependents(some)).toBe(true);
    expect(hasDependents({ ...none, workflows: some.workflows })).toBe(true);
  });
});

describe("unresolved", () => {
  it("lists what still needs a choice: the runs, then each workflow not deactivated", () => {
    expect(unresolved(some, { cancelRuns: false, deactivate: new Set() })).toEqual([
      "runs",
      "workflow:5",
      "workflow:6",
    ]);
    expect(unresolved(some, { cancelRuns: true, deactivate: new Set([5]) })).toEqual([
      "workflow:6",
    ]);
    expect(unresolved(some, { cancelRuns: true, deactivate: new Set([5, 6]) })).toEqual([]);
  });

  it("needs nothing when nothing depends on it", () => {
    expect(unresolved(none, { cancelRuns: false, deactivate: new Set() })).toEqual([]);
  });
});

describe("resolutions", () => {
  it("names the listed runs to cancel, and the workflows to deactivate", () => {
    expect(resolutions(some, { cancelRuns: true, deactivate: new Set([6]) })).toEqual({
      cancel_agent_runs: [1],
      cancel_workflow_runs: [2],
      deactivate_workflows: [6],
    });
  });

  it("cancels nothing unless asked, and deactivates only workflows still listed", () => {
    expect(resolutions(some, { cancelRuns: false, deactivate: new Set([5, 99]) })).toEqual({
      cancel_agent_runs: [],
      cancel_workflow_runs: [],
      deactivate_workflows: [5],
    });
  });
});

describe("parseDeleteRefusal", () => {
  it("reads the message and the dependents from a 409", () => {
    const body = JSON.stringify({ detail: { message: "This agent is still in use", dependents: some } });
    expect(parseDeleteRefusal(new ApiError(409, body))).toEqual({
      message: "This agent is still in use",
      dependents: some,
    });
  });

  it("is undefined for anything else", () => {
    expect(parseDeleteRefusal(new ApiError(409, JSON.stringify({ detail: "Nope" })))).toBeUndefined();
    expect(parseDeleteRefusal(new ApiError(500, "<html>"))).toBeUndefined();
    expect(parseDeleteRefusal(new Error("offline"))).toBeUndefined();
  });
});
