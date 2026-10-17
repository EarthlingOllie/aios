import { Badge } from "@/components/ui/badge";
import type { WorkflowRunStatus } from "@/lib/api";
import type { NodeState } from "@/lib/runs";
import { cn } from "@/lib/utils";

// How a run, and each node it reaches, reads: one set of words and colours for both.
export const statusLabel: Record<WorkflowRunStatus | NodeState, string> = {
  queued: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  waiting: "Waiting for you",
  pending: "Not started",
  "not-run": "Didn't run",
};

const runningClass = "border-sky-500 text-sky-700 dark:text-sky-300";
const waitingClass = "border-amber-500 text-amber-700 dark:text-amber-300";

/** Where a run is on one node of its graph. */
export function StateBadge({ state }: { state: NodeState }) {
  return (
    <Badge
      variant="outline"
      className={cn(
        state === "running" && runningClass,
        state === "waiting" && waitingClass,
        state === "succeeded" && "border-emerald-500 text-emerald-700 dark:text-emerald-300",
        state === "failed" && "border-red-500 text-red-700 dark:text-red-300",
      )}
    >
      {state === "running" && (
        <span className="relative flex size-1.5" aria-hidden>
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-sky-500 opacity-75" />
          <span className="relative inline-flex size-1.5 rounded-full bg-sky-500" />
        </span>
      )}
      {statusLabel[state]}
    </Badge>
  );
}

/** Where a whole run is. */
export function RunStatusBadge({ status, ...props }: { status: WorkflowRunStatus } & object) {
  return (
    <Badge
      variant={
        status === "failed" ? "destructive" : status === "succeeded" ? "secondary" : "outline"
      }
      className={cn(status === "running" && runningClass, status === "waiting" && waitingClass)}
      {...props}
    >
      {statusLabel[status]}
    </Badge>
  );
}
