import { describe, expect, it } from "vitest";

import type { RunSummary, Turn } from "@/lib/api";
import {
  formatCounters,
  formatDuration,
  formatElapsed,
  formatTokens,
  runCounters,
  turnCounters,
} from "@/lib/agent-runs";

function run(changes: Partial<RunSummary> = {}): RunSummary {
  return {
    id: 1,
    agent_id: 2,
    agent_version_id: 3,
    agent_version: 1,
    title: "Find sources",
    message_count: 4,
    trigger_id: null,
    status: null,
    started_at: null,
    finished_at: null,
    tool_call_count: null,
    input_tokens: null,
    output_tokens: null,
    chat_id: null,
    chat_name: null,
    created_by: null,
    created_at: "2026-10-14T09:00:00Z",
    updated_at: "2026-10-14T09:00:05Z",
    ...changes,
  };
}

describe("formatDuration", () => {
  it("reads in seconds, minutes or hours depending on how long it took", () => {
    expect(formatDuration("2026-10-14T09:00:00Z", "2026-10-14T09:00:04.200Z")).toBe("4.2s");
    expect(formatDuration("2026-10-14T09:00:00Z", "2026-10-14T09:03:05Z")).toBe("3m 5s");
    expect(formatDuration("2026-10-14T09:00:00Z", "2026-10-14T10:02:30Z")).toBe("1h 2m");
  });

  it("measures to now while it's still going", () => {
    const now = Date.parse("2026-10-14T09:00:10Z");
    expect(formatDuration("2026-10-14T09:00:00Z", null, now)).toBe("10.0s");
  });

  it("never goes negative when the clocks disagree", () => {
    const now = Date.parse("2026-10-14T08:59:59Z");
    expect(formatDuration("2026-10-14T09:00:00Z", null, now)).toBe("0.0s");
  });
});

describe("formatElapsed", () => {
  const from = "2026-10-14T09:00:00Z";
  const at = (iso: string) => Date.parse(iso);

  it("counts whole seconds, so a clock ticking each second moves a step at a time", () => {
    expect(formatElapsed(from, null, at("2026-10-14T09:00:00.400Z"))).toBe("0s");
    expect(formatElapsed(from, null, at("2026-10-14T09:00:07.900Z"))).toBe("7s");
  });

  it("reads in minutes and seconds, then hours and minutes, padded so it doesn't jump about", () => {
    expect(formatElapsed(from, null, at("2026-10-14T09:03:05Z"))).toBe("3m 05s");
    expect(formatElapsed(from, null, at("2026-10-14T10:02:30Z"))).toBe("1h 02m");
  });

  it("stops at the end once it has ended, whatever the time now", () => {
    const later = at("2026-10-14T12:00:00Z");
    expect(formatElapsed(from, "2026-10-14T09:00:42Z", later)).toBe("42s");
  });

  it("never goes negative when the clocks disagree", () => {
    expect(formatElapsed(from, null, at("2026-10-14T08:59:58Z"))).toBe("0s");
  });
});

describe("formatTokens", () => {
  it("shows small counts whole and large ones rounded with a unit", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(365)).toBe("365");
    expect(formatTokens(1234)).toBe("1.2k");
    expect(formatTokens(48_000)).toBe("48k");
    expect(formatTokens(2_345_678)).toBe("2.3M");
  });
});

describe("formatCounters", () => {
  it("names one tool call, or several, with the tokens short or in full", () => {
    expect(formatCounters({ status: "running", toolCalls: 1, tokens: 600 })).toBe("1 tool call · 600 tokens");
    expect(formatCounters({ status: "running", toolCalls: 2, tokens: 1234 }, true)).toBe(
      "2 tool calls · 1.2k tokens",
    );
  });
});

describe("runCounters", () => {
  it("is null for a run from before counters were kept", () => {
    expect(runCounters(run())).toBeNull();
  });

  it("adds input and output tokens up, as bills do", () => {
    const tracked = run({
      status: "succeeded",
      started_at: "2026-10-14T09:00:00Z",
      finished_at: "2026-10-14T09:00:04.200Z",
      tool_call_count: 2,
      input_tokens: 350,
      output_tokens: 15,
    });
    expect(runCounters(tracked)).toEqual({ status: "succeeded", toolCalls: 2, tokens: 365 });
  });

  it("counts a run that's running but hasn't reached a model step yet as zero so far", () => {
    const starting = run({ status: "running", started_at: "2026-10-14T09:00:00Z" });
    expect(runCounters(starting)).toEqual({ status: "running", toolCalls: 0, tokens: 0 });
  });
});

describe("turnCounters", () => {
  const turn: Turn = {
    id: 1,
    invocation_id: 2,
    agent_version_id: 3,
    agent_version: 1,
    status: "succeeded",
    error: null,
    started_at: "2026-10-14T09:00:00Z",
    finished_at: "2026-10-14T09:00:04.200Z",
    model_calls: 3,
    tool_calls: 2,
    input_tokens: 1000,
    output_tokens: 30,
  };

  it("says how long it took, its model calls, tool calls and tokens", () => {
    expect(turnCounters(turn)).toBe("4.2s · 3 model calls · 2 tool calls · 1,030 tokens");
  });

  it("counts one model call as one, and a running turn up to now", () => {
    const running = { ...turn, status: "running" as const, finished_at: null, model_calls: 1 };
    const now = Date.parse("2026-10-14T09:00:10Z");
    expect(turnCounters(running, now)).toBe("10.0s · 1 model call · 2 tool calls · 1,030 tokens");
  });
});
