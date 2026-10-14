import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
  type Viewport,
  useReactFlow,
} from "@xyflow/react";
import { Bot, Workflow as WorkflowIcon } from "lucide-react";

import "@xyflow/react/dist/style.css";

import { Badge } from "@/components/ui/badge";
import type { NodePosition, WorkflowEdge, WorkflowNode } from "@/lib/api";
import {
  fitGraph,
  layoutGraph,
  MIN_EDITING_ZOOM,
  NODE_HEIGHT,
  NODE_WIDTH,
  startKeys,
} from "@/lib/graph";
import { cn } from "@/lib/utils";

/** What's selected on the canvas, shown in the inspector. */
export type Selection =
  | { type: "node"; key: string }
  | { type: "edge"; source: string; target: string };

export const edgeId = (e: WorkflowEdge) => `${e.source}->${e.target}`;

type CardData = { node: WorkflowNode; start: boolean; editing: boolean };
type CardNode = Node<CardData, "workflowNode">;

/** A node: what it runs (an agent or a workflow), at which version, under its key. */
function NodeCard({ data, selected }: NodeProps<CardNode>) {
  const { node, start, editing } = data;
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
      <Handle type="target" position={Position.Left} isConnectable={editing} />
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
      <Handle type="source" position={Position.Right} isConnectable={editing} />
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
const deleteKeys = ["Delete", "Backspace"];

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

/** Moves the view to `viewport` whenever it changes after the first render. */
function FitOnChange({ viewport }: { viewport: Viewport }) {
  const { setViewport } = useReactFlow();
  const shown = useRef(viewport);
  useEffect(() => {
    if (viewport === shown.current) return;
    shown.current = viewport;
    void setViewport(viewport);
  }, [viewport, setViewport]);
  return null;
}

/** What the canvas does with edits, when it's editable. */
export type CanvasEditing = {
  /** New positions by node key: dragging a selection moves several nodes at once. */
  onMove: (positions: Record<string, NodePosition>) => void;
  onConnect: (source: string, target: string) => void;
  canConnect: (source: string, target: string) => boolean;
  /** The Delete or Backspace key, on the selected node or edge. */
  onDelete: (selection: Selection) => void;
};

/**
 * A workflow version's graph, laid out left to right in the order it runs.
 *
 * When `editing` is given, nodes can be dragged, connected by dragging from a node's right
 * handle to another's left, and deleted with the keyboard; `layout` must then place every node.
 */
export function WorkflowCanvas({
  nodes,
  edges,
  layout,
  selection,
  onSelect,
  editing,
  className,
  children,
}: {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  /** Saved positions by node key; the rest are laid out automatically. */
  layout: Record<string, NodePosition>;
  selection: Selection | null;
  onSelect: (selection: Selection | null) => void;
  editing?: CanvasEditing;
  className?: string;
  /** Overlaid on the canvas, e.g. a legend. */
  children?: ReactNode;
}) {
  const keys = useMemo(() => nodes.map((n) => n.key), [nodes]);
  // While editing, nodes are dragged every frame: their positions are used as they are.
  const isEditing = !!editing;
  const positions = useMemo(
    () => (isEditing ? layout : layoutGraph(keys, edges, layout)),
    [isEditing, keys, edges, layout],
  );
  const starts = useMemo(() => new Set(startKeys(keys, edges)), [keys, edges]);
  // Adding or removing a node or an edge lays the graph out again (see `lib/editor`), which can
  // move nodes out of view, so the canvas is sized and the view placed again then: when the nodes
  // and edges change, by node index, so renaming a key or dragging a node doesn't move the view.
  const shape = useMemo(() => {
    const index = new Map(keys.map((key, i) => [key, i]));
    return `${keys.length}:${edges.map((e) => `${index.get(e.source)}>${index.get(e.target)}`)}`;
  }, [keys, edges]);

  // React Flow re-adopts (and re-renders) every node and edge it's handed as a new object, so
  // only build new ones when what they show changes, not on every render of the page around us.
  // So too for selecting: only what's selected (or deselected) is built anew.
  const unselectedNodes = useMemo<CardNode[]>(
    () =>
      nodes.map((node) => ({
        id: node.key,
        type: "workflowNode",
        position: positions[node.key],
        data: { node, start: starts.has(node.key), editing: isEditing },
        draggable: isEditing,
        connectable: isEditing,
      })),
    [nodes, positions, starts, isEditing],
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
  const onNodesChange = useStableCallback((changes: NodeChange<CardNode>[]) => {
    const moves: Record<string, NodePosition> = {};
    for (const change of changes) {
      if (change.type === "position" && change.position) moves[change.id] = change.position;
    }
    if (Object.keys(moves).length) editing?.onMove(moves);
  });
  const onConnect = useStableCallback(({ source, target }: Connection) =>
    editing?.onConnect(source, target),
  );
  const isValidConnection = useStableCallback(
    ({ source, target }: Edge | Connection) => !!editing?.canConnect(source, target),
  );
  const onDelete = useStableCallback(
    ({ nodes: deleted, edges: deletedEdges }: { nodes: CardNode[]; edges: Edge[] }) => {
      // Deleting a node takes its edges with it, so it's reported alone.
      if (deleted.length) editing?.onDelete({ type: "node", key: deleted[0].id });
      else if (deletedEdges.length) {
        const [e] = deletedEdges;
        editing?.onDelete({ type: "edge", source: e.source, target: e.target });
      }
    },
  );

  // The canvas is sized, and the graph placed on it, once its width is known (before it's first
  // drawn), and not again as nodes are dragged about; while editing, again when its shape changes.
  const ref = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState<ReturnType<typeof fitGraph> & { shape: string }>();
  useLayoutEffect(() => {
    if (fit && (!isEditing || fit.shape === shape)) return;
    const minZoom = isEditing ? MIN_EDITING_ZOOM : undefined;
    setFit({ ...fitGraph(Object.values(positions), ref.current!.clientWidth, minZoom), shape });
  }, [fit, isEditing, shape, positions]);
  if (!fit) return <div ref={ref} className={cn("rounded-md border", className)} />;

  return (
    <div
      ref={ref}
      className={cn("relative rounded-md border", className)}
      style={{ height: fit.height }}
    >
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        colorMode="system"
        defaultViewport={fit.viewport}
        nodesConnectable={isEditing}
        nodesDraggable={isEditing}
        elementsSelectable
        // One thing is selected at a time (`selection`): no Shift-click or box selection.
        multiSelectionKeyCode={null}
        selectionKeyCode={null}
        deleteKeyCode={isEditing ? deleteKeys : null}
        onNodesChange={onNodesChange}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        onDelete={onDelete}
        onNodeClick={onNodeClick}
        onEdgeClick={onEdgeClick}
        onPaneClick={onPaneClick}
        proOptions={proOptions}
      >
        <Background />
        <Controls showInteractive={false} fitViewOptions={fitViewOptions} />
        {editing && <FitOnChange viewport={fit.viewport} />}
      </ReactFlow>
      {children}
    </div>
  );
}
