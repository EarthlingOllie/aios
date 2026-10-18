import { RunStatusBadge } from "@/components/workflow/status";
import { formatCounters, formatDuration, runCounters } from "@/lib/agent-runs";
import type { RunSummary } from "@/lib/api";

/** Where an agent run is, if it's tracked. */
export function AgentRunStatusBadge({ run, ...props }: { run: RunSummary } & object) {
  return run.status ? <RunStatusBadge status={run.status} {...props} /> : null;
}

/**
 * How the run went and what it cost, in a line: how long its latest turn took, its tool calls
 * and its tokens over all its turns. Or that it's from before these were kept.
 */
export function RunCountersLine({
  run,
  compact = false,
  ...props
}: { run: RunSummary; compact?: boolean; className?: string } & object) {
  const counters = runCounters(run);
  if (!counters) return <span {...props}>Not tracked</span>;
  const parts = [
    run.started_at && formatDuration(run.started_at, run.finished_at),
    formatCounters(counters, compact),
  ].filter(Boolean);
  return <span {...props}>{parts.join(" · ")}</span>;
}
