import { describe, expect, it } from "vitest";

import { describeFilter, listOf, triggerInput } from "@/components/schedules";
import type { Trigger } from "@/lib/api";

describe("event trigger filters", () => {
  it("reads a typed comma-separated list, without the blanks", () => {
    expect(listOf(" application/pdf, ,text/* ,")).toEqual(["application/pdf", "text/*"]);
    expect(listOf("")).toEqual([]);
  });

  it("says what a filter matches, naming sources it knows", () => {
    expect(describeFilter({})).toBe("");
    expect(describeFilter({ content_types: ["application/pdf", "text/*"] })).toBe(
      "of type application/pdf, text/*",
    );
    expect(describeFilter({ sources: [1, 7] }, new Map([[1, "Uploads"]]))).toBe("from Uploads, source 7");
  });
});

describe("triggerInput", () => {
  const base = {
    id: 3,
    target_kind: "agent",
    target_id: 9,
    target_name: "Reader",
    enabled: true,
    input: "",
    description: "",
    next_run_at: null,
    last_fired_at: null,
    last_status: null,
    last_error: null,
    last_run_id: null,
    last_workflow_run_id: null,
    disabled_reason: null,
    created_at: "",
    updated_at: "",
  };

  it("keeps an event trigger's event fields, so pausing one doesn't lose them", () => {
    const t = {
      ...base,
      kind: "event",
      cron: null,
      timezone: null,
      event_type: "document_ingested",
      filter: { content_types: ["text/*"] },
      fire_on_own_events: true,
    } as unknown as Trigger;
    expect(triggerInput(t, { enabled: false })).toEqual({
      kind: "event",
      target_kind: "agent",
      target_id: 9,
      enabled: false,
      input: "",
      cron: null,
      timezone: "UTC",
      event_type: "document_ingested",
      filter: { content_types: ["text/*"] },
      fire_on_own_events: true,
      deliver_to: null,
    });
  });

  it("sends a schedule without event fields", () => {
    const t = {
      ...base,
      kind: "schedule",
      cron: "0 9 * * *",
      timezone: "Europe/London",
      event_type: null,
      filter: {},
      fire_on_own_events: false,
    } as unknown as Trigger;
    expect(triggerInput(t)).not.toHaveProperty("event_type");
  });

  it("keeps the chat it delivers to, by id", () => {
    const t = {
      ...base,
      kind: "schedule",
      cron: "0 9 * * *",
      timezone: "Europe/London",
      deliver_to: { id: 4, name: "Ops", archived: false },
    } as unknown as Trigger;
    expect(triggerInput(t).deliver_to).toBe(4);
    expect(triggerInput(t, { deliver_to: null }).deliver_to).toBeNull();
  });
});
