/**
 * What an agent run records about how it went: its status, how long its latest turn took, and
 * what it cost over all its turns (see the `agent_runs` columns on the API's `RunSummary`).
 *
 * Runs from before this was kept have null for all of it, and are shown as not tracked rather
 * than as zero.
 */
import type { AgentRunStatus, RunSummary } from "@/lib/api";

export type RunCounters = {
  status: AgentRunStatus;
  toolCalls: number;
  /** Input plus output tokens of every model call, as bills count them. */
  tokens: number;
};

/** How long from `from` to `to`, or to now while it's still going. */
export function formatDuration(from: string, to: string | null, now = Date.now()) {
  const seconds = Math.max(0, ((to ? Date.parse(to) : now) - Date.parse(from)) / 1000);
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/** A token count, short enough for a list: whole under a thousand, then 1.2k, 48k, 2.3M. */
export function formatTokens(n: number) {
  const short = (value: number, unit: string) =>
    `${value < 10 ? value.toFixed(1) : Math.round(value)}${unit}`;
  if (n < 1000) return String(n);
  if (n < 1_000_000) return short(n / 1000, "k");
  return short(n / 1_000_000, "M");
}

/** The run's counters, or null if it's from before they were kept. A run that's tracked but
 * hasn't reached a model step yet has nothing to count so far. */
export function runCounters(run: RunSummary): RunCounters | null {
  if (!run.status) return null;
  return {
    status: run.status,
    toolCalls: run.tool_call_count ?? 0,
    tokens: (run.input_tokens ?? 0) + (run.output_tokens ?? 0),
  };
}
