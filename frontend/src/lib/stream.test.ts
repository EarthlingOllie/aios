import { describe, expect, it } from "vitest";

import { parseEvents, retryAfter } from "@/lib/stream";

describe("parseEvents", () => {
  it("reads complete events and keeps an incomplete one for later", () => {
    const { events, rest, retry } = parseEvents(
      'retry: 5000\n\nid: 12\nevent: message\ndata: {"id":12,"body":"Hi"}\n\nevent: ping\ndata: {}\n\nevent: dra',
    );
    expect(events).toEqual([
      { event: "message", data: { id: 12, body: "Hi" }, id: 12 },
      { event: "ping", data: {}, id: null },
    ]);
    expect(rest).toBe("event: dra");
    expect(retry).toBe(5000);
  });

  it("joins multi-line data, skips comments, and keeps data that isn't JSON as text", () => {
    const { events } = parseEvents(": hello\n\nevent: note\ndata: one\ndata: two\n\n");
    expect(events).toEqual([{ event: "note", data: "one\ntwo", id: null }]);
  });

  it("reads events split across chunks once the rest arrives", () => {
    const first = parseEvents("event: draft\ndata: {\"text\":");
    expect(first.events).toEqual([]);
    const second = parseEvents(first.rest + '"Hel"}\n\n');
    expect(second.events).toEqual([{ event: "draft", data: { text: "Hel" }, id: null }]);
  });
});

describe("retryAfter", () => {
  it("is the refusal's Retry-After, else the fallback", () => {
    expect(retryAfter(new Response(null, { status: 503, headers: { "Retry-After": "5" } }), 1)).toBe(5000);
    expect(retryAfter(new Response(null, { status: 503 }), 1234)).toBe(1234);
  });
});
