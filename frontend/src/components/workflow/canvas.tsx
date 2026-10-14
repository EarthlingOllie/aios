import { useMemo, type ReactNode } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { Bot, Workflow as WorkflowIcon } from "lucide-react";

import "@xyflow/react/dist/style.css";

import { Badge } from "@/components/ui/badge";
import type { NodePosition, WorkflowEdge, WorkflowNode } from "@/lib/api";
import { layoutGraph, NODE_HEIGHT, NODE_WIDTH, startKeys } from "@/lib/graph";
import { cn } from "@/lib/utils";

/** What's selected on the canvas, shown in the inspector. */
export type Selection =
  | { type: "node"; key: string }
  | { type: "edge"; source: string; target: string };

export const edgeId = (e: WorkflowEdge) => `${e.source}->${e.target}`;

export function sameSelection(a: Selection | null, b: Selection | null) {
  return JSON.stringify(a) === JSON.stringify(b);
}

type CardData = { node: WorkflowNode; start: boolean };
type CardNode = Node<CardData, "workflowNode">;

/** A node: what it runs (an agent or a workflow), at which version, under its key. */
function NodeCard({ data, selected }: NodeProps<CardNode>) {
  const { node, start } = data;
  const Icon = node.kind === "agent" ? Bot : WorkflowIcon;
  const update = node.latest_version !== null && node.latest_version > node.version;
  return (
    <div
      className={cn(
        "bg-card text-card-foreground flex flex-col justify-center gap-1 rounded-lg border px-3 py-2 shadow-xs",
        selected && "ring-ring ring-2",
      )}
      style={{ width: NODE_WIDTH, height: NODE_HEIGHT }}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div className="flex min-w-0 items-center gap-2">
        <Icon className="text-muted-foreground size-4 shrink-0" aria-hidden />
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{node.name}</span>
        <Badge variant="outline" className="font-mono">
          v{node.version}
        </Badge>
      </div>
      <div className="text-muted-foreground flex min-w-0 items-center gap-1.5 text-xs">
        <span className="sr-only">{node.kind === "agent" ? "Agent" : "Workflow"}</span>
        <span className="min-w-0 flex-1 truncate font-mono">{node.key}</span>
        {start && <Badge variant="secondary">Start</Badge>}
        {node.kind === "workflow" && <Badge variant="secondary">Workflow</Badge>}
        {update && <Badge variant="outline">v{node.latest_version} available</Badge>}
        {node.latest_version === null && <Badge variant="destructive">deleted</Badge>}
      </div>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

const nodeTypes = { workflowNode: NodeCard };

/** A workflow version's graph, laid out left to right in the order it runs. */
export function WorkflowCanvas({
  nodes,
  edges,
  layout,
  selection,
  onSelect,
  className,
  children,
}: {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  /** Saved positions by node key; the rest are laid out automatically. */
  layout: Record<string, NodePosition>;
  selection: Selection | null;
  onSelect: (selection: Selection | null) => void;
  className?: string;
  /** Overlaid on the canvas, e.g. a legend. */
  children?: ReactNode;
}) {
  const keys = useMemo(() => nodes.map((n) => n.key), [nodes]);
  const positions = useMemo(() => layoutGraph(keys, edges, layout), [keys, edges, layout]);
  const starts = useMemo(() => new Set(startKeys(keys, edges)), [keys, edges]);

  const flowNodes: CardNode[] = nodes.map((node) => ({
    id: node.key,
    type: "workflowNode",
    position: positions[node.key],
    data: { node, start: starts.has(node.key) },
    selected: selection?.type === "node" && selection.key === node.key,
    draggable: false,
    connectable: false,
  }));
  const flowEdges: Edge[] = edges.map((e) => ({
    id: edgeId(e),
    source: e.source,
    target: e.target,
    markerEnd: { type: MarkerType.ArrowClosed },
    selected:
      selection?.type === "edge" && selection.source === e.source && selection.target === e.target,
  }));

  return (
    <div className={cn("relative h-[28rem] rounded-md border", className)}>
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        colorMode="system"
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
        nodesConnectable={false}
        nodesDraggable={false}
        elementsSelectable
        onNodeClick={(_, n) => onSelect({ type: "node", key: n.id })}
        onEdgeClick={(_, e) => onSelect({ type: "edge", source: e.source, target: e.target })}
        onPaneClick={() => onSelect(null)}
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
      {children}
    </div>
  );
}
