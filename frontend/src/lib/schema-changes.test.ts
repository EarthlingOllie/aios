import { describe, expect, it } from "vitest";

import type { SchemaChangeRequest } from "@/lib/api";
import { requestSummary } from "@/lib/schema-changes";

function request(fields: Partial<SchemaChangeRequest>): SchemaChangeRequest {
  return {
    id: "r1",
    data_store_id: "s1",
    status: "pending",
    summary: "Create table data.contacts",
    reason: "To keep track of people",
    sql: 'CREATE TABLE "data"."contacts" ();',
    ops: [],
    run: { agent_id: 1, run_id: 2, title: "go" },
    tool_call_id: "call_1",
    note: null,
    error: null,
    created_at: "2026-10-17T09:00:00Z",
    decided_at: null,
    applied_at: null,
    resumed_at: null,
    can_decide: true,
    ...fields,
  };
}

describe("requestSummary", () => {
  it("waits for the owner while pending", () => {
    expect(requestSummary(request({}))).toMatchObject({ label: "Waiting for you", inFlight: false });
  });

  it("is in flight while applying, and while the run carries on", () => {
    expect(requestSummary(request({ status: "approved" })).inFlight).toBe(true);
    const applied = requestSummary(request({ status: "applied" }));
    expect(applied).toMatchObject({ label: "Applied", inFlight: true });
    expect(applied.message).toContain("carrying on");
    const resumed = requestSummary(request({ status: "applied", resumed_at: "2026-10-17T09:01:00Z" }));
    expect(resumed).toMatchObject({ message: "Applied to your data store.", inFlight: false });
  });

  it("isn't in flight once decided when no run waits on it", () => {
    expect(requestSummary(request({ status: "rejected", run: null })).inFlight).toBe(false);
  });

  it("gives the owner's note, and why it failed or no longer applies", () => {
    const done = { resumed_at: "2026-10-17T09:01:00Z" };
    expect(requestSummary(request({ status: "rejected", note: "Use a sheet", ...done })).message).toBe(
      "Rejected: Use a sheet",
    );
    expect(requestSummary(request({ status: "rejected", ...done })).message).toBe(
      "Rejected. Nothing changed.",
    );
    expect(requestSummary(request({ status: "failed", error: "lock timeout", ...done }))).toMatchObject({
      label: "Failed",
      variant: "destructive",
      message: "Applying it failed, and nothing changed: lock timeout",
    });
    expect(
      requestSummary(request({ status: "stale", error: "`data.contacts` already exists", ...done }))
        .message,
    ).toContain("`data.contacts` already exists");
  });
});
