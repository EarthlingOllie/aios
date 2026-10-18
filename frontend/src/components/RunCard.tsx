import { useState, type ReactNode } from "react";
import { Link } from "react-router";

import { WorkflowCanvas } from "@/components/workflow/canvas";
import { RunStatusBadge } from "@/components/workflow/status";
import type { ChatInvocation, NodePosition, WorkflowVersion } from "@/lib/api";
import { edgeStates, isActive, nodesDone, nodeStates } from "@/lib/runs";

/** How tall a card's graph is drawn (px), whatever its size. */
export const RUN_CARD_GRAPH_HEIGHT = 160;

// The card has no saved layout to hand: its graph is laid out afresh. One object, so it's stable.
const noLayout: Record<string, NodePosition> = {};
const noSelect = () => undefined;

/**
 * The card under a mentioned workflow's acknowledgement, following the run the mention started:
 * its status, the run's graph with each node's state as the run page draws it (still, at a fixed
 * height, with no inspector) and how many nodes are done. Clicking anywhere on it opens the run.
 * A run that couldn't start shows why; one since deleted says so.
 */
export function RunCard({ invocation }: { invocation: ChatInvocation | undefined }) {
  const run = invocation?.workflow_run ?? null;
  // A version never changes: keep the first copy, so a poll doesn't have the graph laid out again.
  const [version, setVersion] = useState<WorkflowVersion>();
  if (run && run.version.id !== version?.id) setVersion(run.version);

  if (!invocation) return <Frame>Starting the run…</Frame>;
  if (!run || !version) {
    return (
      <Frame status={<RunStatusBadge status={invocation.status} />}>
        {invocation.run_deleted ? (
          <p className="text-muted-foreground">The run has been deleted.</p>
        ) : invocation.status === "failed" ? (
          <p role="alert" className="text-destructive break-words">
            Couldn't start the run: {invocation.error}
          </p>
        ) : (
          <p className="text-muted-foreground">Starting the run…</p>
        )}
      </Frame>
    );
  }
  const keys = version.nodes.map((n) => n.key);
  const { done, total } = nodesDone(keys, run.steps);
  return (
    <Link
      to={`/workflows/${invocation.target_id}/runs/${run.id}`}
      aria-label={`Open run #${run.id}`}
      data-testid="run-card"
      className="hover:bg-accent/40 block max-w-xl rounded-md border p-2 text-sm"
    >
      <div className="mb-2 flex items-center gap-2">
        <span className="font-medium">Run #{run.id}</span>
        <span className="text-muted-foreground flex-1 text-xs">
          {done} of {total} nodes done
        </span>
        <RunStatusBadge status={run.status} data-testid="run-card-status" />
      </div>
      {/* A picture of the graph: the click is the card's. */}
      <div className="pointer-events-none">
        <WorkflowCanvas
          nodes={version.nodes}
          edges={version.edges}
          layout={noLayout}
          selection={null}
          onSelect={noSelect}
          still={RUN_CARD_GRAPH_HEIGHT}
          run={{
            nodes: nodeStates(keys, run.steps, [], isActive(run.status)),
            edges: edgeStates(version.edges, run.steps, []),
          }}
        />
      </div>
    </Link>
  );
}

function Frame({ status, children }: { status?: ReactNode; children: ReactNode }) {
  return (
    <div data-testid="run-card" className="flex max-w-xl items-start gap-2 rounded-md border p-2 text-sm">
      <div className="min-w-0 flex-1">{children}</div>
      {status}
    </div>
  );
}
