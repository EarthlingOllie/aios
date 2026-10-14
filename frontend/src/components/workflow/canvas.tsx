import { useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
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
import { fitGraph, layoutGraph, NODE_HEIGHT, NODE_WIDTH, startKeys } from "@/lib/graph";
import { cn } from "@/lib/utils";

/** What's selected on the canvas, shown in the inspector. */
export type Selection =
  | { type: "node"; key: string }
  | { type: "edge"; source: string; target: string };

export const edgeId = (e: WorkflowEdge) => `${e.source}->${e.target}`;

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

// A selected edge stands out as a selected node does: drawn thicker, in the foreground colour,
// arrowhead included. (Markers are sized in stroke widths: this keeps it in proportion.)
const selectedEdgeStyle = { stroke: "var(--foreground)", strokeWidth: 2.5 };
const selectedMarker = { color: selectedEdgeStyle.stroke, width: 7.5, height: 7.5 };

const nodeTypes = { workflowNode: NodeCard };
// For the controls' button: the whole graph, however small that makes it.
const fitViewOptions = { padding: 0.2, maxZoom: 1 };
const proOptions = { hideAttribution: true };

/** `fn` as a function that stays the same across renders but always calls the latest `fn`, so
 * React Flow, which re-renders every node and edge when a handler changes, keeps them. */
function useStableCallback<A extends unknown[], R>(fn: (...args: A) => R) {
  const latest = useRef(fn);
  useLayoutEffect(() => {
    latest.current = fn;
  });
  return useCallback((...args: A) => latest.current(...args), []);
}

/** `items` with the one whose id is `id` marked selected, reusing the rest as they are. */
function withSelected<T extends { id: string }>(items: T[], id: string | undefined) {
  return items.map((item) => (item.id === id ? { ...item, selected: true } : item));
}

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

  // React Flow re-adopts (and re-renders) every node and edge it's handed as a new object, so
  // only build new ones when what they show changes, not on every render of the page around us.
  // So too for selecting: only what's selected (or deselected) is built anew.
  const unselectedNodes = useMemo<CardNode[]>(
    () =>
      nodes.map((node) => ({
        id: node.key,
        type: "workflowNode",
        position: positions[node.key],
        data: { node, start: starts.has(node.key) },
        draggable: false,
        connectable: false,
      })),
    [nodes, positions, starts],
  );
  const unselectedEdges = useMemo<Edge[]>(
    () =>
      edges.map((e) => ({
        id: edgeId(e),
        source: e.source,
        target: e.target,
        markerEnd: { type: MarkerType.ArrowClosed },
      })),
    [edges],
  );
  const selectedNode = selection?.type === "node" ? selection.key : undefined;
  const selectedEdge = selection?.type === "edge" ? edgeId(selection) : undefined;
  const flowNodes = useMemo(
    () => withSelected(unselectedNodes, selectedNode),
    [unselectedNodes, selectedNode],
  );
  const flowEdges = useMemo(
    () =>
      withSelected(unselectedEdges, selectedEdge).map((e) =>
        e.selected
          ? {
              ...e,
              style: { ...e.style, ...selectedEdgeStyle },
              markerEnd: { type: MarkerType.ArrowClosed, ...selectedMarker },
            }
          : e,
      ),
    [unselectedEdges, selectedEdge],
  );

  const onNodeClick = useStableCallback((_: unknown, n: CardNode) =>
    onSelect({ type: "node", key: n.id }),
  );
  const onEdgeClick = useStableCallback((_: unknown, e: Edge) =>
    onSelect({ type: "edge", source: e.source, target: e.target }),
  );
  const onPaneClick = useStableCallback(() => onSelect(null));

  // The canvas is sized, and the graph placed on it, once its width is known (before it's first
  // drawn), and not again as nodes are dragged about.
  const ref = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState<ReturnType<typeof fitGraph>>();
  useLayoutEffect(() => {
    if (!fit) setFit(fitGraph(Object.values(positions), ref.current!.clientWidth));
  }, [fit, positions]);
  if (!fit) return <div ref={ref} className={cn("rounded-md border", className)} />;

  return (
    <div className={cn("relative rounded-md border", className)} style={{ height: fit.height }}>
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        colorMode="system"
        defaultViewport={fit.viewport}
        nodesConnectable={false}
        nodesDraggable={false}
        elementsSelectable
        onNodeClick={onNodeClick}
        onEdgeClick={onEdgeClick}
        onPaneClick={onPaneClick}
        proOptions={proOptions}
      >
        <Background />
        <Controls showInteractive={false} fitViewOptions={fitViewOptions} />
      </ReactFlow>
      {children}
    </div>
  );
}
