import type { ReactNode } from "react";
import { Link } from "react-router";
import { ArrowRight, Bot, Workflow as WorkflowIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { VersionBadge } from "@/components/versions";
import type { Selection } from "@/components/workflow/canvas";
import type { WorkflowEdge, WorkflowNode } from "@/lib/api";

/** The selected node's or edge's configuration, beside the canvas. */
export function Inspector({
  nodes,
  edges,
  selection,
  children,
}: {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  selection: Selection | null;
  /** Extra content for the selection, e.g. a run's step. */
  children?: ReactNode;
}) {
  const node = selection?.type === "node" ? nodes.find((n) => n.key === selection.key) : undefined;
  const edge =
    selection?.type === "edge"
      ? edges.find((e) => e.source === selection.source && e.target === selection.target)
      : undefined;
  return (
    <aside
      aria-label="Inspector"
      className="flex min-w-0 flex-col gap-4 rounded-md border p-4 text-sm"
    >
      {node ? (
        <NodeDetails node={node} edges={edges} />
      ) : edge ? (
        <EdgeDetails edge={edge} edges={edges} />
      ) : (
        <p className="text-muted-foreground">Select a node or an edge to see its configuration.</p>
      )}
      {children}
    </aside>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <dt className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
        {label}
      </dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function NodeDetails({ node, edges }: { node: WorkflowNode; edges: WorkflowEdge[] }) {
  const Icon = node.kind === "agent" ? Bot : WorkflowIcon;
  const href = node.kind === "agent" ? `/agents/${node.target_id}` : `/workflows/${node.target_id}`;
  const before = edges.filter((e) => e.target === node.key).map((e) => e.source);
  const after = edges.filter((e) => e.source === node.key).map((e) => e.target);
  return (
    <>
      <header className="flex items-center gap-2">
        <Icon className="text-muted-foreground size-4" aria-hidden />
        <span className="font-semibold">{node.kind === "agent" ? "Agent" : "Workflow"} node</span>
      </header>
      <dl className="flex flex-col gap-3">
        <Row label="Key">
          <span className="font-mono">{node.key}</span>
        </Row>
        <Row label="Runs">
          <span className="flex flex-wrap items-center gap-2">
            {/* A deleted target's page is gone, so it's named without a link. */}
            {node.latest_version === null ? (
              <span className="font-medium">{node.name}</span>
            ) : (
              <Link to={href} className="font-medium hover:underline">
                {node.name}
              </Link>
            )}
            <VersionBadge version={node.version} />
            {node.latest_version === null ? (
              <Badge variant="destructive">deleted</Badge>
            ) : (
              node.latest_version > node.version && (
                <Badge variant="outline">v{node.latest_version} available</Badge>
              )
            )}
          </span>
        </Row>
        <Row label="Input">
          {before.length === 0
            ? "Start: receives the workflow's input."
            : `After ${listing(before)}: receives ${before.length === 1 ? "its output" : "their outputs"}.`}
        </Row>
        <Row label="Output">
          {after.length === 0
            ? "End: its output is part of the workflow's output."
            : `Then ${listing(after)}.`}
        </Row>
      </dl>
    </>
  );
}

function EdgeDetails({ edge, edges }: { edge: WorkflowEdge; edges: WorkflowEdge[] }) {
  const others = edges
    .filter((e) => e.target === edge.target && e.source !== edge.source)
    .map((e) => e.source);
  return (
    <>
      <header className="flex items-center gap-2">
        <ArrowRight className="text-muted-foreground size-4" aria-hidden />
        <span className="font-semibold">Edge</span>
      </header>
      <dl className="flex flex-col gap-3">
        <Row label="Connects">
          <span className="font-mono">
            {edge.source} → {edge.target}
          </span>
        </Row>
        <Row label="Passes">
          When {edge.source} finishes, its output is passed to {edge.target}.
          {others.length > 0 &&
            ` ${edge.target} also waits for ${listing(others)}, and gets their outputs too.`}
        </Row>
      </dl>
    </>
  );
}

/** "a", "a and b", "a, b and c". */
export function listing(items: string[]) {
  return items.length <= 1
    ? (items[0] ?? "")
    : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}
