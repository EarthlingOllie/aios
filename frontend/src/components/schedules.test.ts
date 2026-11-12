import { describe, expect, it } from "vitest";

import { describeFilter, filterFor, listOf, triggerInput } from "@/components/schedules";
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

  it("says what table, schema and chat filters match", () => {
    expect(describeFilter({ tables: ["orders"], operations: ["insert", "delete"] })).toBe(
      "to orders by insert, delete",
    );
    expect(
      describeFilter(
        { chat_id: 4, author_kinds: ["user"], keywords: ["outage", "down"] },
        new Map(),
        new Map([[4, "Ops"]]),
      ),
    ).toBe('in Ops written by user saying "outage" or "down"');
    expect(describeFilter({ chat_id: 9, mentions: ["agent:3"] })).toBe("in chat 9 mentioning agent:3");
  });

  it("sends only the event type's own filter fields", () => {
    const typed = { contentTypes: "text/*", tables: "orders, data.invoices", mentions: "agent:3", keywords: "outage," };
    const filter = { sources: [1], operations: ["insert" as const], chat_id: 4, author_kinds: ["agent" as const] };
    expect(filterFor("document_ingested", filter, typed)).toEqual({ sources: [1], content_types: ["text/*"] });
    expect(filterFor("table_written", filter, typed)).toEqual({
      tables: ["orders", "data.invoices"],
      operations: ["insert"],
    });
    expect(filterFor("schema_change_applied", filter, typed)).toEqual({ tables: ["orders", "data.invoices"] });
    expect(filterFor("chat_message", filter, typed)).toEqual({
      chat_id: 4,
      author_kinds: ["agent"],
      mentions: ["agent:3"],
      keywords: ["outage"],
    });
    expect(filterFor("webhook_received", filter, typed)).toEqual({});
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
      debounce_seconds: 60,
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
      debounce_seconds: 60,
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
