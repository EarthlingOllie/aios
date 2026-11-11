import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { WorkflowCanvas } from "@/components/workflow/canvas";
import { RunStatusBadge } from "@/components/workflow/status";
import { formatCounters, formatElapsed, runCounters } from "@/lib/agent-runs";
import type { ChatAgentRun, ChatInvocation, NodePosition, WorkflowVersion } from "@/lib/api";
import { edgeStates, isActive, nodesDone, nodeStates } from "@/lib/runs";

/** How tall a card's graph is drawn (px), whatever its size. */
export const RUN_CARD_GRAPH_HEIGHT = 160;

// The card has no saved layout to hand: its graph is laid out afresh. One object, so it's stable.
const noLayout: Record<string, NodePosition> = {};
const noSelect = () => undefined;

/** How often a running agent card's elapsed time ticks (ms). */
const TICK_MS = 1000;

/**
 * The card under a mentioned agent's or workflow's acknowledgement, following the run the
 * mention started. A workflow's shows its status, the run's graph with each node's state as the
 * run page draws it (still, at a fixed height, with no inspector) and how many nodes are done;
 * an agent's, its status (waiting, while its schema change request waits for a decision), the time elapsed (ticking while it runs) and its tool calls and tokens
 * so far. Clicking anywhere on it opens the run. A run that couldn't start shows why; one since
 * deleted says so. Under it, behind a disclosure, is the brief the handoff gave the run, so
 * anyone can see exactly what it was told.
 */
export function RunCard({ invocation }: { invocation: ChatInvocation | undefined }) {
  return (
    <>
      <Card invocation={invocation} />
      {invocation?.brief && <Brief brief={invocation.brief} />}
    </>
  );
}

function Card({ invocation }: { invocation: ChatInvocation | undefined }) {
  if (invocation?.target_kind === "agent") {
    const run = invocation.agent_run;
    if (!run) return <Unstarted invocation={invocation} />;
    return <AgentRunCard agentId={invocation.target_id} run={run} />;
  }
  return <WorkflowRunCard invocation={invocation} />;
}

/** What the handoff told the run of the chat, before the message: closed until asked for. */
function Brief({ brief }: { brief: string }) {
  return (
    <details data-testid="run-card-brief" className="mt-1 max-w-xl text-sm">
      <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
        Brief
      </summary>
      <p className="text-muted-foreground mt-1 border-l-2 pl-2 break-words whitespace-pre-wrap">{brief}</p>
    </details>
  );
}

/** A card with no run to show: briefing or starting, couldn't start, or deleted since. */
function Unstarted({ invocation }: { invocation: ChatInvocation }) {
  return (
    <Frame status={<RunStatusBadge status={invocation.status} data-testid="run-card-status" />}>
      {invocation.run_deleted ? (
        <p className="text-muted-foreground">The run has been deleted.</p>
      ) : invocation.status === "failed" ? (
        <p role="alert" className="text-destructive break-words">
          Couldn't start the run: {invocation.error}
        </p>
      ) : invocation.status === "briefing" ? (
        <p className="text-muted-foreground">Briefing the run on the chat…</p>
      ) : (
        <p className="text-muted-foreground">Starting the run…</p>
      )}
    </Frame>
  );
}

/** The time now, ticking every `TICK_MS` while `ticking`. */
function useNow(ticking: boolean) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!ticking) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(timer);
  }, [ticking]);
  return now;
}

function AgentRunCard({ agentId, run }: { agentId: number; run: ChatAgentRun }) {
  const running = run.status === "running";
  const now = useNow(running);
  const counters = runCounters(run);
  return (
    <Link
      to={`/agents/${agentId}/runs/${run.id}`}
      aria-label={`Open run #${run.id}`}
      data-testid="run-card"
      className="hover:bg-accent/40 flex max-w-xl items-center gap-2 rounded-md border p-2 text-sm"
    >
      <span className="font-medium">Run #{run.id}</span>
      {run.started_at && (
        <span data-testid="run-card-elapsed" className="text-muted-foreground tabular-nums">
          {formatElapsed(run.started_at, running ? null : run.finished_at, now)}
        </span>
      )}
      <span data-testid="run-card-counters" className="text-muted-foreground flex-1 text-xs">
        {counters && formatCounters(counters, true)}
      </span>
      {run.waiting ? (
        <RunStatusBadge status="waiting" data-testid="run-card-status" />
      ) : (
        run.status && <RunStatusBadge status={run.status} data-testid="run-card-status" />
      )}
    </Link>
  );
}

function WorkflowRunCard({ invocation }: { invocation: ChatInvocation | undefined }) {
  const run = invocation?.workflow_run ?? null;
  // A version never changes: keep the first copy, so a poll doesn't have the graph laid out again.
  const [version, setVersion] = useState<WorkflowVersion>();
  if (run && run.version.id !== version?.id) setVersion(run.version);

  if (!invocation) return <Frame>Starting the run…</Frame>;
  if (!run || !version) return <Unstarted invocation={invocation} />;
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
