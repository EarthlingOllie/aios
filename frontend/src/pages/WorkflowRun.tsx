import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ChevronRight, MessageSquare, Trash2, Workflow as WorkflowIcon } from "lucide-react";

import {
  DetailPage,
  ErrorBanner,
  formatDate,
  Placeholder,
  Section,
  useLoad,
} from "@/components/crud";
import { Button } from "@/components/ui/button";
import { VersionBadge } from "@/components/versions";
import { WorkflowCanvas, type Selection } from "@/components/workflow/canvas";
import { Inspector } from "@/components/workflow/inspector";
import { RunStatusBadge, StateBadge } from "@/components/workflow/status";
import {
  api,
  describeError,
  type NodePosition,
  type WorkflowNode,
  type WorkflowRun,
  type WorkflowRunStep,
  type WorkflowVersion,
} from "@/lib/api";
import { edgeStates, isActive, type NodeState, nodeStates, stepAt } from "@/lib/runs";

/** How often a run that's still going is re-read. */
const POLL_MS = 1000;

function duration(from: string, to: string | null) {
  const seconds = Math.max(0, ((to ? Date.parse(to) : Date.now()) - Date.parse(from)) / 1000);
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/** One level of the graph being shown: the run's workflow, or a subworkflow inside it. */
type Level = {
  /** The workflow node's key, or null for the run's own workflow. */
  key: string | null;
  name: string;
  version: WorkflowVersion;
  layout: Record<string, NodePosition>;
};

export function WorkflowRunPage() {
  const id = Number(useParams().workflowId);
  const runId = Number(useParams().runId);
  // Keyed so drill-down and selection reset when another run is opened.
  return <WorkflowRunView key={`${id}/${runId}`} id={id} runId={runId} />;
}

function WorkflowRunView({ id, runId }: { id: number; runId: number }) {
  const navigate = useNavigate();
  const workflow = useLoad(useCallback(() => api.getWorkflow(id), [id]));
  const run = useLoad(useCallback(() => api.getWorkflowRun(id, runId), [id, runId]));
  // The subworkflows opened below the run's workflow, outermost first.
  const [opened, setOpened] = useState<Level[]>([]);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [error, setError] = useState<string>();

  // Follow a run that's still going: re-read it shortly after each read.
  const status = run.data?.status;
  const { reload } = run;
  useEffect(() => {
    if (!status || !isActive(status)) return;
    const timer = setTimeout(reload, POLL_MS);
    return () => clearTimeout(timer);
  }, [status, run.data, reload]);

  if (run.error) return <Placeholder>{run.error}</Placeholder>;
  if (!run.data || !workflow.data) return null;
  const r = run.data;
  const levels: Level[] = [
    { key: null, name: workflow.data.name, version: r.version, layout: workflow.data.layout },
    ...opened,
  ];
  const level = levels[levels.length - 1];
  const prefix = opened.map((l) => l.key!);
  const keys = level.version.nodes.map((n) => n.key);
  const active = isActive(r.status);
  const states = nodeStates(keys, r.steps, prefix, active);

  const goTo = (depth: number) => {
    setOpened(opened.slice(0, depth));
    setSelection(null);
  };
  const open = async (node: WorkflowNode) => {
    setError(undefined);
    try {
      const [version, sub] = await Promise.all([
        api.getWorkflowVersion(node.target_id, node.version),
        // Only for where its nodes are drawn; they're laid out afresh without it.
        api.getWorkflow(node.target_id).catch(() => undefined),
      ]);
      setOpened([
        ...opened,
        { key: node.key, name: node.name, version, layout: sub?.layout ?? {} },
      ]);
      setSelection(null);
    } catch (err) {
      setError(`Couldn't open ${node.name}: ${describeError(err)}`);
    }
  };
  const onDelete = async () => {
    if (!window.confirm("Delete this run, and the agent conversations it started?")) return;
    try {
      await api.deleteWorkflowRun(id, runId);
      navigate(`/workflows/${id}`);
    } catch (err) {
      setError(describeError(err));
    }
  };

  const selected =
    selection?.type === "node"
      ? level.version.nodes.find((n) => n.key === selection.key)
      : undefined;

  return (
    <DetailPage
      wide
      title={
        <span className="flex flex-wrap items-center gap-2">
          <Link to={`/workflows/${id}`} className="hover:underline">
            {workflow.data.name}
          </Link>
          <span className="text-muted-foreground font-normal">run #{r.id}</span>
          <VersionBadge version={r.workflow_version} />
          <RunStatusBadge status={r.status} data-testid="run-status" />
        </span>
      }
      subtitle={
        r.started_at
          ? `Started ${formatDate(r.started_at)} · ${duration(r.started_at, r.finished_at)}`
          : `Queued ${formatDate(r.created_at)}`
      }
      actions={
        <Button
          variant="outline"
          size="sm"
          onClick={onDelete}
          disabled={r.status === "running"}
          title={r.status === "running" ? "A run can be deleted once it has finished" : undefined}
          aria-label="Delete run"
        >
          <Trash2 />
        </Button>
      }
    >
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {r.error && (
        <div
          role="alert"
          className="border-destructive/50 text-destructive rounded-md border px-3 py-2 text-sm"
        >
          {r.error}
        </div>
      )}
      <div className="grid gap-4 md:grid-cols-2">
        <TextBlock label="Input">{r.input}</TextBlock>
        {r.output !== null ? (
          <TextBlock label="Output">{r.output}</TextBlock>
        ) : (
          <p className="text-muted-foreground self-center text-sm">
            {active ? "The output appears here when the run finishes." : "No output."}
          </p>
        )}
      </div>
      <Section title="Graph">
        {opened.length > 0 && (
          <nav aria-label="Subworkflow path" className="flex flex-wrap items-center gap-1 text-sm">
            {levels.map((l, depth) => (
              <span key={depth} className="flex items-center gap-1">
                {depth > 0 && <ChevronRight className="text-muted-foreground size-3.5" />}
                {depth < levels.length - 1 ? (
                  <button className="hover:underline" onClick={() => goTo(depth)}>
                    {depth === 0 ? l.name : l.key}
                  </button>
                ) : (
                  <span className="font-medium">
                    {l.key} <span className="text-muted-foreground font-normal">({l.name})</span>
                  </span>
                )}
              </span>
            ))}
          </nav>
        )}
        <div className="@container">
          <div className="grid gap-4 @4xl:grid-cols-[1fr_18rem]">
            <WorkflowCanvas
              // Remounted per level, so each graph is fitted to the view.
              key={prefix.join("/")}
              nodes={level.version.nodes}
              edges={level.version.edges}
              layout={level.layout}
              selection={selection}
              onSelect={setSelection}
              run={{
                nodes: states,
                edges: edgeStates(level.version.edges, r.steps, prefix),
              }}
            />
            <Inspector
              nodes={level.version.nodes}
              edges={level.version.edges}
              selection={selection}
            >
              {selected && (
                <StepDetails
                  node={selected}
                  step={stepAt(r.steps, [...prefix, selected.key])}
                  state={states[selected.key]}
                  onOpen={() => open(selected)}
                />
              )}
            </Inspector>
          </div>
        </div>
      </Section>
      <Section title="Steps">
        <StepList run={r} />
      </Section>
    </DetailPage>
  );
}

function TextBlock({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section aria-label={label} className="flex min-w-0 flex-col gap-1">
      <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
        {label}
      </h3>
      <pre className="bg-muted/50 max-h-64 overflow-y-auto rounded-md border p-3 text-sm whitespace-pre-wrap">
        {children}
      </pre>
    </section>
  );
}

/** The selected node's step in this run, in the inspector. */
function StepDetails({
  node,
  step,
  state,
  onOpen,
}: {
  node: WorkflowNode;
  step: WorkflowRunStep | undefined;
  state: NodeState;
  onOpen: () => void;
}) {
  return (
    <div className="flex flex-col gap-3 border-t pt-3">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
          In this run
        </span>
        <StateBadge state={state} />
      </div>
      {step ? (
        <>
          <p className="text-muted-foreground text-xs">
            Started {formatDate(step.started_at)} · {duration(step.started_at, step.finished_at)}
          </p>
          {step.error && <p className="text-destructive text-sm break-words">{step.error}</p>}
          <TextBlock label="Step input">{step.input}</TextBlock>
          {step.output !== null && <TextBlock label="Step output">{step.output}</TextBlock>}
          {step.agent_id !== null && step.agent_run_id !== null && (
            <Button asChild size="sm" variant="outline">
              <Link to={`/agents/${step.agent_id}/runs/${step.agent_run_id}`}>
                <MessageSquare />
                Open the conversation
              </Link>
            </Button>
          )}
        </>
      ) : (
        <p className="text-muted-foreground text-sm">
          {state === "waiting"
            ? "Waiting: it starts once every node before it has succeeded."
            : "Didn't run: a node before it failed, or the run stopped first."}
        </p>
      )}
      {node.kind === "workflow" && (
        <Button size="sm" variant="outline" onClick={onOpen}>
          <WorkflowIcon />
          Open subworkflow
        </Button>
      )}
    </div>
  );
}

/** Every step of the run, nested ones included, in the order they started. */
function StepList({ run }: { run: WorkflowRun }) {
  if (run.steps.length === 0) {
    return <p className="text-muted-foreground text-sm">No node has started yet.</p>;
  }
  return (
    <ol className="divide-y rounded-md border text-sm">
      {run.steps.map((step) => (
        <li key={step.id} className="flex items-center gap-3 px-3 py-2">
          <span className="min-w-0 flex-1 truncate font-mono">{step.path.join(" / ")}</span>
          <span className="text-muted-foreground text-xs">
            {duration(step.started_at, step.finished_at)}
          </span>
          <StateBadge state={step.status} />
        </li>
      ))}
    </ol>
  );
}
