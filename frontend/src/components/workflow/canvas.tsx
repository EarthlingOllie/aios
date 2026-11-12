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
import { StateBadge } from "@/components/workflow/status";
import type { NodePosition, WorkflowEdge, WorkflowNode } from "@/lib/api";
import {
  edgeId,
  fitGraph,
  layoutGraph,
  MIN_EDITING_ZOOM,
  MIN_STILL_ZOOM,
  NODE_HEIGHT,
  NODE_WIDTH,
  startKeys,
} from "@/lib/graph";
import { describeCondition } from "@/lib/conditions";
import type { EdgeState, NodeState } from "@/lib/runs";
import { cn } from "@/lib/utils";

/** What's selected on the canvas, shown in the inspector. */
export type Selection =
  | { type: "node"; key: string }
  | { type: "edge"; source: string; target: string };

type CardData = { node: WorkflowNode; start: boolean; editing: boolean; state?: NodeState };
type CardNode = Node<CardData, "workflowNode">;

/** A node: what it runs (an agent or a workflow), at which version, under its key. */
function NodeCard({ data, selected }: NodeProps<CardNode>) {
  const { node, start, editing, state } = data;
  const Icon = node.kind === "agent" ? Bot : WorkflowIcon;
  const update = node.latest_version !== null && node.latest_version > node.version;
  return (
    <div
      className={cn(
        "bg-card text-card-foreground flex flex-col justify-center gap-1 rounded-lg border px-3 py-2 shadow-xs",
        state && stateClass[state],
        selected && (state ? selectedStateClass[state] : "ring-ring ring-2"),
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
        {state ? <StateBadge state={state} /> : start && <Badge variant="secondary">Start</Badge>}
        {!state && node.kind === "workflow" && <Badge variant="secondary">Workflow</Badge>}
        {!state && update && <Badge variant="outline">v{node.latest_version} available</Badge>}
        {!state && node.latest_version === null && <Badge variant="destructive">deleted</Badge>}
      </div>
      <Handle type="source" position={Position.Right} isConnectable={editing} />
    </div>
  );
}

// A run's progress on its nodes: where it's been, where it is, and what it never reached.
const stateClass: Record<NodeState, string> = {
  running: "border-sky-500 ring-4 ring-sky-500/25",
  succeeded: "border-emerald-500",
  failed: "border-red-500 ring-2 ring-red-500/30",
  waiting: "border-amber-500 ring-4 ring-amber-500/25",
  pending: "opacity-60 border-dashed",
  "not-run": "opacity-40 border-dashed",
  skipped: "opacity-40 border-dashed grayscale bg-muted",
};

// Selected, a node on a run keeps its state's colours: its outline and ring are drawn thicker.
const selectedStateClass: Record<NodeState, string> = {
  running: "border-2 ring-6",
  succeeded: "border-2 ring-4 ring-emerald-500/25",
  failed: "border-2 ring-4",
  waiting: "border-2 ring-6",
  pending: "border-2 ring-ring ring-2",
  "not-run": "border-2 ring-ring ring-2",
  skipped: "border-2 ring-ring ring-2",
};

const edgeStyle: Record<EdgeState, { stroke?: string; strokeWidth?: number; opacity?: number }> = {
  live: { stroke: "var(--color-sky-500)", strokeWidth: 2 },
  taken: { stroke: "var(--color-emerald-500)", strokeWidth: 2 },
  idle: { opacity: 0.4 },
};

// For tests and styling: the state as an attribute of the node's or edge's element. (`data-*`
// attributes are valid on any element, but object literals aren't checked as JSX props are.)
const dataState = (state: string) => ({ "data-state": state }) as Record<string, string>;

/** `e` as it stands out when selected, as a selected node does: drawn thicker, arrowhead included,
 * in its run's colour if it has one, else the foreground's, and fully even if the run didn't take
 * it. (Markers are sized in stroke widths: this keeps the arrowhead in proportion.) */
function selectedEdge(e: Edge): Edge {
  const stroke = e.style?.stroke ?? "var(--foreground)";
  const strokeWidth = Number(e.style?.strokeWidth ?? 1) + 1.5;
  return {
    ...e,
    style: { ...e.style, stroke, strokeWidth, opacity: 1 },
    markerEnd: { type: MarkerType.ArrowClosed, color: stroke, width: 7.5, height: 7.5 },
  };
}

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
 * When `still` is, it's a picture `still` pixels tall: the graph fitted into it, with nothing to
 * pan, zoom, select or focus (e.g. a chat's run card, whose click opens the run).
 */
export function WorkflowCanvas({
  nodes,
  edges,
  layout,
  selection,
  onSelect,
  editing,
  run,
  still,
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
  /** A run's progress, drawn on the graph: each node's state, and each edge's by edge id. */
  run?: { nodes: Record<string, NodeState>; edges: Record<string, EdgeState> };
  /** A fixed height, for a graph that's only looked at. */
  still?: number;
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
  // So too for selecting: only what's selected (or deselected) is built anew. A run's states are
  // rebuilt by each poll, so they're compared by what they say.
  // Each node's size is given, as it's fixed: React Flow hides a node it's handed without one
  // until it has measured it again, and one handed over just as it's first measured stays hidden.
  const runKey = run && JSON.stringify(run);
  const unselectedNodes = useMemo<CardNode[]>(
    () =>
      nodes.map((node) => ({
        id: node.key,
        type: "workflowNode",
        measured: { width: NODE_WIDTH, height: NODE_HEIGHT },
        position: positions[node.key],
        data: {
          node,
          start: starts.has(node.key),
          editing: isEditing,
          state: run?.nodes[node.key],
        },
        domAttributes: run ? dataState(run.nodes[node.key]) : undefined,
        draggable: isEditing,
        connectable: isEditing,
      })),
    [nodes, positions, starts, isEditing, runKey],
  );
  const unselectedEdges = useMemo<Edge[]>(
    () =>
      edges.map((e) => {
        const state = run?.edges[edgeId(e)];
        return {
          id: edgeId(e),
          source: e.source,
          target: e.target,
          markerEnd: { type: MarkerType.ArrowClosed, color: state && edgeStyle[state].stroke },
          animated: state === "live",
          // A conditional edge says when it's taken.
          label: describeCondition(e.condition) ?? undefined,
          labelStyle: { fontSize: 10 },
          style: state && edgeStyle[state],
          domAttributes: state ? dataState(state) : undefined,
        };
      }),
    [edges, runKey],
  );
  const selectedNode = selection?.type === "node" ? selection.key : undefined;
  const selectedEdgeId = selection?.type === "edge" ? edgeId(selection) : undefined;
  const flowNodes = useMemo(
    () => withSelected(unselectedNodes, selectedNode),
    [unselectedNodes, selectedNode],
  );
  const flowEdges = useMemo(
    () =>
      withSelected(unselectedEdges, selectedEdgeId).map((e) => (e.selected ? selectedEdge(e) : e)),
    [unselectedEdges, selectedEdgeId],
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
  // React Flow's own delete key listens on the whole page, and only spares text fields: on any
  // other control (a button, our select) it would delete what's selected. So it's handled here,
  // on the node or edge that has focus (which clicking it gives it).
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!editing || !deleteKeys.includes(e.key)) return;
    const focused = (e.target as Element).closest(".react-flow__node, .react-flow__edge");
    const id = focused?.getAttribute("data-id");
    if (!id) return;
    e.preventDefault();
    if (focused!.classList.contains("react-flow__node"))
      editing.onDelete({ type: "node", key: id });
    else {
      const edge = edges.find((edge) => edgeId(edge) === id);
      if (edge) editing.onDelete({ type: "edge", source: edge.source, target: edge.target });
    }
  };

  // The canvas is sized, and the graph placed on it, once its width is known (before it's first
  // drawn), and not again as nodes are dragged about; while editing, again when its shape changes.
  const ref = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState<ReturnType<typeof fitGraph> & { shape: string }>();
  useLayoutEffect(() => {
    if (fit && (!isEditing || fit.shape === shape)) return;
    const minZoom = isEditing ? MIN_EDITING_ZOOM : still ? MIN_STILL_ZOOM : undefined;
    const width = ref.current!.clientWidth;
    setFit({ ...fitGraph(Object.values(positions), width, minZoom, still), shape });
  }, [fit, isEditing, still, shape, positions]);
  if (!fit) return <div ref={ref} className={cn("rounded-md border", className)} />;

  return (
    <div
      ref={ref}
      className={cn("relative rounded-md border", className)}
      style={{ height: fit.height }}
      onKeyDown={onKeyDown}
    >
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        colorMode="system"
        defaultViewport={fit.viewport}
        nodesConnectable={isEditing}
        nodesDraggable={isEditing}
        elementsSelectable={!still}
        nodesFocusable={!still}
        edgesFocusable={!still}
        panOnDrag={!still}
        zoomOnScroll={!still}
        zoomOnPinch={!still}
        zoomOnDoubleClick={!still}
        // A still picture leaves the wheel to the page around it.
        preventScrolling={!still}
        // One thing is selected at a time (`selection`): no Shift-click or box selection.
        multiSelectionKeyCode={null}
        selectionKeyCode={null}
        deleteKeyCode={null}
        onNodesChange={onNodesChange}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        onNodeClick={onNodeClick}
        onEdgeClick={onEdgeClick}
        onPaneClick={onPaneClick}
        proOptions={proOptions}
      >
        <Background />
        {!still && <Controls showInteractive={false} fitViewOptions={fitViewOptions} />}
        {editing && <FitOnChange viewport={fit.viewport} />}
      </ReactFlow>
      {children}
    </div>
  );
}
