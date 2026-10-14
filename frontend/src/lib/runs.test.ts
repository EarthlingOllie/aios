import { describe, expect, it } from "vitest";

import type { WorkflowRunStep } from "@/lib/api";
import { edgeStates, isActive, nodeStates, stepAt } from "@/lib/runs";

function step(path: string[], status: WorkflowRunStep["status"]): WorkflowRunStep {
  return {
    id: path.length * 100 + path.join("").length,
    path,
    kind: "agent",
    status,
    input: "in",
    output: status === "succeeded" ? "out" : null,
    error: status === "failed" ? "boom" : null,
    agent_id: 1,
    agent_run_id: 2,
    started_at: "2026-10-14T09:00:00Z",
    finished_at: status === "running" ? null : "2026-10-14T09:00:05Z",
  };
}

const keys = ["research", "write", "review"];
const edges = [
  { source: "research", target: "write" },
  { source: "write", target: "review" },
];

describe("isActive", () => {
  it("is true while queued or running", () => {
    expect(isActive("queued")).toBe(true);
    expect(isActive("running")).toBe(true);
    expect(isActive("succeeded")).toBe(false);
    expect(isActive("failed")).toBe(false);
  });
});

describe("stepAt", () => {
  it("finds the step for a node at a path", () => {
    const steps = [step(["review"], "running"), step(["review", "critic"], "running")];
    expect(stepAt(steps, ["review", "critic"])).toBe(steps[1]);
    expect(stepAt(steps, ["critic"])).toBeUndefined();
  });
});

describe("nodeStates", () => {
  it("gives each node its step's status, or waiting / not run when it has none", () => {
    const steps = [step(["research"], "succeeded"), step(["write"], "running")];
    expect(nodeStates(keys, steps, [], true)).toEqual({
      research: "succeeded",
      write: "running",
      review: "waiting",
    });
    // Once the run has ended, a node with no step never ran.
    expect(nodeStates(keys, [step(["research"], "failed")], [], false)).toEqual({
      research: "failed",
      write: "not-run",
      review: "not-run",
    });
  });

  it("looks only at steps directly inside the given path", () => {
    const steps = [step(["review"], "running"), step(["review", "critic"], "succeeded")];
    expect(nodeStates(["critic"], steps, ["review"], true)).toEqual({ critic: "succeeded" });
  });
});

describe("edgeStates", () => {
  it("marks edges whose source succeeded as taken, and live while the target runs", () => {
    const steps = [step(["research"], "succeeded"), step(["write"], "running")];
    expect(edgeStates(edges, steps, [])).toEqual({
      "research->write": "live",
      "write->review": "idle",
    });
    const done = [
      ...steps.slice(0, 1),
      step(["write"], "succeeded"),
      step(["review"], "succeeded"),
    ];
    expect(edgeStates(edges, done, [])).toEqual({
      "research->write": "taken",
      "write->review": "taken",
    });
  });
});
