import { describe, expect, it } from "vitest";

import type { SchemaChangeRequest } from "@/lib/api";
import { approvalNeeds, requestSummary } from "@/lib/schema-changes";

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
    needs_role: "editor",
    can_decide: true,
    confirm_with: null,
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
    expect(resumed).toMatchObject({ message: "Applied to the organisation's data store.", inFlight: false });
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

  it("says an expired request counts as rejected, and the agent carries on", () => {
    const expired = requestSummary(request({ status: "expired" }));
    expect(expired).toMatchObject({ label: "Expired", variant: "outline", inFlight: true });
    expect(expired.message).toBe(
      "Nobody decided in time, so it expired, which counts as rejected. Nothing changed. The agent is carrying on.",
    );
    const done = requestSummary(request({ status: "expired", resumed_at: "2026-10-17T09:01:00Z" }));
    expect(done).toMatchObject({ inFlight: false });
    expect(done.message).toBe(
      "Nobody decided in time, so it expired, which counts as rejected. Nothing changed.",
    );
  });

  it("says a cancelled request's run is gone", () => {
    const cancelled = requestSummary(
      request({ status: "cancelled", run: null, resumed_at: "2026-10-17T09:01:00Z" }),
    );
    expect(cancelled).toMatchObject({
      label: "Cancelled",
      variant: "outline",
      message: "Cancelled: the run that asked was deleted or stopped. Nothing changed.",
      inFlight: false,
    });
  });

  it("names when a request was decided, expired or cancelled", () => {
    const at = { decided_at: "2026-10-17T09:01:00Z" };
    expect(requestSummary(request({ status: "rejected", ...at })).decidedLabel).toBe("Decided");
    expect(requestSummary(request({ status: "expired", ...at })).decidedLabel).toBe("Expired");
    expect(requestSummary(request({ status: "cancelled", ...at })).decidedLabel).toBe("Cancelled");
  });
});

describe("approvalNeeds", () => {
  it("names the least role that may decide a pending request", () => {
    expect(approvalNeeds(request({ needs_role: "editor" }))).toBe("Needs an editor or an admin to approve");
    expect(approvalNeeds(request({ needs_role: "admin" }))).toBe("Needs an admin to approve");
  });

  it("says nothing once it's decided", () => {
    expect(approvalNeeds(request({ status: "applied", needs_role: "admin" }))).toBeNull();
  });
});
