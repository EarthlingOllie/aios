import { useState } from "react";
import { ArrowUpCircle, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { WorkflowCanvas, type Selection } from "@/components/workflow/canvas";
import { Inspector, Row } from "@/components/workflow/inspector";
import type { NodePosition, WorkflowNode } from "@/lib/api";
import {
  addNode,
  canConnect,
  connect,
  keyError,
  moveNode,
  removeEdge,
  removeNode,
  renameNode,
  retarget,
  updateToLatest,
  type Draft,
  type Target,
} from "@/lib/editor";
import { NODE_HEIGHT, NODE_WIDTH } from "@/lib/graph";
import { cn } from "@/lib/utils";

const selectClass = "border-input w-full rounded-md border bg-transparent px-2 py-1.5 text-sm";
const targetValue = (t: { kind: string; id: number }) => `${t.kind}:${t.id}`;

/** The agents and workflows a node can run, grouped by kind, as `<option>`s. */
function TargetOptions({ targets }: { targets: Target[] }) {
  return (
    <>
      {(["agent", "workflow"] as const).map((kind) => {
        const of = targets.filter((t) => t.kind === kind);
        return (
          of.length > 0 && (
            <optgroup key={kind} label={kind === "agent" ? "Agents" : "Workflows"}>
              {of.map((t) => (
                <option key={targetValue(t)} value={targetValue(t)}>
                  {t.name}
                </option>
              ))}
            </optgroup>
          )
        );
      })}
    </>
  );
}

/**
 * Where a new node goes: to the right of the selected node, or of the rightmost one, moved down
 * until it doesn't overlap another node.
 */
function freeSpot(draft: Draft, near: string | undefined): NodePosition {
  const placed = Object.values(draft.positions);
  if (placed.length === 0) return { x: 0, y: 0 };
  const from =
    (near ? draft.positions[near] : undefined) ??
    placed.reduce((right, p) => (p.x > right.x ? p : right), placed[0]);
  const spot = { x: from.x + NODE_WIDTH + 64, y: from.y };
  const overlaps = () =>
    placed.some(
      (p) => Math.abs(p.x - spot.x) < NODE_WIDTH && Math.abs(p.y - spot.y) < NODE_HEIGHT + 16,
    );
  while (overlaps()) spot.y += NODE_HEIGHT + 32;
  return spot;
}

/** The graph being edited: the canvas, a picker for adding nodes, and an editable inspector. */
export function GraphEditor({
  draft,
  onChange,
  targets,
}: {
  draft: Draft;
  onChange: (draft: Draft) => void;
  /** What nodes can run: the user's agents, and their workflows other than this one. */
  targets: Target[] | undefined;
}) {
  const [selection, setSelection] = useState<Selection | null>(null);
  const [adding, setAdding] = useState("");
  const selectedKey = selection?.type === "node" ? selection.key : undefined;

  const onAdd = () => {
    const target = targets?.find((t) => targetValue(t) === adding);
    if (!target) return;
    const next = addNode(draft, target, freeSpot(draft, selectedKey));
    onChange(next);
    setSelection({ type: "node", key: next.nodes.at(-1)!.key });
  };

  const onDelete = (sel: Selection) => {
    onChange(
      sel.type === "node" ? removeNode(draft, sel.key) : removeEdge(draft, sel.source, sel.target),
    );
    setSelection(null);
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="add-node" className="text-sm font-medium">
          Add node
        </label>
        <select
          id="add-node"
          className={cn(selectClass, "w-auto min-w-56")}
          value={adding}
          onChange={(e) => setAdding(e.target.value)}
        >
          <option value="" disabled>
            {targets?.length === 0 ? "You have no agents yet" : "Choose an agent or workflow…"}
          </option>
          <TargetOptions targets={targets ?? []} />
        </select>
        <Button type="button" size="sm" variant="outline" onClick={onAdd} disabled={!adding}>
          <Plus />
          Add
        </Button>
      </div>
      <div className="@container">
        <div className="grid gap-4 @4xl:grid-cols-[1fr_18rem]">
          <WorkflowCanvas
            nodes={draft.nodes}
            edges={draft.edges}
            layout={draft.positions}
            selection={selection}
            onSelect={setSelection}
            editing={{
              onMove: (key, position) => onChange(moveNode(draft, key, position)),
              onConnect: (source, target) => onChange(connect(draft, source, target)),
              canConnect: (source, target) => canConnect(draft, source, target) === null,
              onDelete,
            }}
          />
          <Inspector
            nodes={draft.nodes}
            edges={draft.edges}
            selection={selection}
            editNode={(node) => ({
              // Keyed so the field starts from the node's key when another node is selected.
              key: (
                <KeyField
                  key={node.key}
                  node={node}
                  others={draft.nodes.filter((n) => n !== node).map((n) => n.key)}
                  onRename={(to) => {
                    onChange(renameNode(draft, node.key, to));
                    setSelection({ type: "node", key: to });
                  }}
                />
              ),
              controls: (
                <NodeControls
                  key={node.key}
                  node={node}
                  draft={draft}
                  targets={targets ?? []}
                  onChange={onChange}
                  onRemove={() => onDelete({ type: "node", key: node.key })}
                />
              ),
            })}
            editEdge={(edge) => (
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => onDelete({ type: "edge", ...edge })}
              >
                <Trash2 />
                Remove edge
              </Button>
            )}
          />
        </div>
      </div>
    </div>
  );
}

function KeyField({
  node,
  others,
  onRename,
}: {
  node: WorkflowNode;
  others: string[];
  onRename: (key: string) => void;
}) {
  const [value, setValue] = useState(node.key);
  const [error, setError] = useState<string | null>(null);
  const commit = () => {
    if (value === node.key) return setError(null);
    const problem = keyError(value, others);
    setError(problem);
    if (!problem) onRename(value);
  };
  return (
    <Row label="Key">
      <label htmlFor="node-key" className="sr-only">
        Key
      </label>
      <Input
        id="node-key"
        className="font-mono"
        value={value}
        aria-invalid={!!error}
        onChange={(e) => setValue(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          }
        }}
      />
      <span className="text-muted-foreground mt-1 block text-xs">
        Names the node in this workflow and its runs.
      </span>
      {error && <span className="text-destructive mt-1 block text-xs">{error}</span>}
    </Row>
  );
}

function NodeControls({
  node,
  draft,
  targets,
  onChange,
  onRemove,
}: {
  node: WorkflowNode;
  draft: Draft;
  targets: Target[];
  onChange: (draft: Draft) => void;
  onRemove: () => void;
}) {
  const [to, setTo] = useState("");
  const others = draft.nodes.filter((n) => n.key !== node.key).map((n) => n.key);
  const refusals = others.map((key) => [key, canConnect(draft, node.key, key)] as const);
  const current = targetValue({ kind: node.kind, id: node.target_id });
  return (
    <div className="flex flex-col gap-3 border-t pt-3">
      <div className="flex flex-col gap-1">
        <label htmlFor="node-target" className="text-xs font-medium">
          Runs
        </label>
        <select
          id="node-target"
          className={selectClass}
          value={current}
          onChange={(e) => {
            const target = targets.find((t) => targetValue(t) === e.target.value);
            if (target) onChange(retarget(draft, node.key, target));
          }}
        >
          {/* What it runs may have been deleted since, or be this workflow's own older version. */}
          {!targets.some((t) => targetValue(t) === current) && (
            <option value={current} disabled>
              {node.name}
            </option>
          )}
          <TargetOptions targets={targets} />
        </select>
      </div>
      {node.latest_version !== null && node.latest_version > node.version && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => onChange(updateToLatest(draft, node.key))}
        >
          <ArrowUpCircle />
          Update to v{node.latest_version}
        </Button>
      )}
      <div className="flex flex-col gap-1">
        <label htmlFor="node-connect" className="text-xs font-medium">
          Connect to
        </label>
        <div className="flex gap-2">
          <select
            id="node-connect"
            className={selectClass}
            value={to}
            onChange={(e) => setTo(e.target.value)}
          >
            <option value="">Choose a node…</option>
            {refusals
              .filter(([, refusal]) => refusal === null)
              .map(([key]) => (
                <option key={key} value={key}>
                  {key}
                </option>
              ))}
          </select>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!to}
            onClick={() => {
              onChange(connect(draft, node.key, to));
              setTo("");
            }}
          >
            Connect
          </Button>
        </div>
        {refusals.some(([, refusal]) => refusal) && (
          <ul className="text-muted-foreground text-xs">
            {refusals
              .filter(([, refusal]) => refusal)
              .map(([key, refusal]) => (
                <li key={key}>
                  <span className="font-mono">{key}</span>: {refusal}
                </li>
              ))}
          </ul>
        )}
      </div>
      <Button type="button" size="sm" variant="outline" onClick={onRemove}>
        <Trash2 />
        Remove node
      </Button>
    </div>
  );
}
