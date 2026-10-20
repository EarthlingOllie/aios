import { useMemo, useState } from "react";
import { ArrowUpCircle, Plus, Trash2 } from "lucide-react";
import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, type SelectGroup } from "@/components/ui/select";
import { WorkflowCanvas, type Selection } from "@/components/workflow/canvas";
import { Inspector, Row } from "@/components/workflow/inspector";
import type { WorkflowNode } from "@/lib/api";
import {
  addNode,
  canConnect,
  connect,
  keyError,
  moveNodes,
  removeEdge,
  removeNode,
  renameNode,
  retarget,
  updateToLatest,
  type Draft,
  type Target,
} from "@/lib/editor";

const targetValue = (t: { kind: string; id: number }) => `${t.kind}:${t.id}`;

/** The agents and workflows a node can run, grouped by kind; those it can't, shown disabled. */
const targetGroups = (targets: Target[]): SelectGroup[] =>
  (["agent", "workflow"] as const).map((kind) => ({
    label: kind === "agent" ? "Agents" : "Workflows",
    options: targets
      .filter((t) => t.kind === kind)
      .map((t) => ({
        value: targetValue(t),
        label: t.name,
        disabled: !!t.unavailable,
        badge: t.unavailable,
      })),
  }));

/** The graph being edited: the canvas, a picker for adding nodes, and an editable inspector. */
export function GraphEditor({
  draft,
  onChange,
  targets,
}: {
  draft: Draft;
  onChange: (draft: Draft) => void;
  /** What nodes can run: the organisation's agents and workflows, with why some workflows can't. */
  targets: Target[] | undefined;
}) {
  const [selection, setSelection] = useState<Selection | null>(null);
  const [adding, setAdding] = useState("");

  const onAdd = () => {
    const target = targets?.find((t) => targetValue(t) === adding);
    if (!target || target.unavailable) return;
    const next = addNode(draft, target);
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
        <Select
          id="add-node"
          className="min-w-56"
          value={adding}
          onChange={setAdding}
          groups={targetGroups(targets ?? [])}
          placeholder={
            targets?.every((t) => t.unavailable)
              ? "Nothing to add yet"
              : "Choose an agent or workflow…"
          }
        />
        <Button type="button" size="sm" variant="outline" onClick={onAdd} disabled={!adding}>
          <Plus />
          Add
        </Button>
        {targets && !targets.some((t) => t.kind === "agent") && (
          <p className="text-muted-foreground text-sm">
            You have no agents yet.{" "}
            <Link to="/agents/new" className="underline">
              Create one
            </Link>
            .
          </p>
        )}
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
              onMove: (positions) => onChange(moveNodes(draft, positions)),
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
              // Keyed by place, not key, so renaming the node on the key field's blur doesn't
              // remount the buttons mid-click (renames keep a node's place; removals deselect it).
              controls: (
                <NodeControls
                  key={draft.nodes.indexOf(node)}
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
        onChange={(e) => {
          const next = e.target.value;
          setValue(next);
          // So the form won't submit, dropping the edit, while the key is invalid.
          e.target.setCustomValidity((next !== node.key && keyError(next, others)) || "");
        }}
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
  // Dragging changes only the positions, re-rendering on every frame; skip the cycle checks then.
  const { nodes, edges } = draft;
  const refusals = useMemo(
    () =>
      nodes
        .filter((n) => n.key !== node.key)
        .map((n) => [n.key, canConnect({ nodes, edges, positions: {} }, node.key, n.key)] as const),
    [nodes, edges, node.key],
  );
  // Only the nodes it can lead to; why the others can't is listed below the picker.
  const connectable = refusals
    .filter(([, refusal]) => refusal === null)
    .map(([key]) => ({ value: key, label: key }));
  const current = targetValue({ kind: node.kind, id: node.target_id });
  // Whether its latest version would make this workflow contain itself, as far as the list says.
  const listed = targets.find((t) => targetValue(t) === current);
  const loop = listed?.version_id === node.latest_version_id ? listed.unavailable : undefined;
  return (
    <div className="flex flex-col gap-3 border-t pt-3">
      <div className="flex flex-col gap-1">
        <label htmlFor="node-target" className="text-xs font-medium">
          Runs
        </label>
        <Select
          id="node-target"
          value={current}
          onChange={(value) => {
            const target = targets.find((t) => targetValue(t) === value);
            if (target && !target.unavailable) onChange(retarget(draft, node.key, target));
          }}
          groups={targetGroups(targets)}
          // What it runs may have been deleted since.
          placeholder={node.name}
        />
      </div>
      {node.latest_version !== null && node.latest_version > node.version && (
        <div className="flex flex-col gap-1">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!!loop}
            aria-describedby={loop ? "node-update-refusal" : undefined}
            onClick={() => onChange(updateToLatest(draft, node.key))}
          >
            <ArrowUpCircle />
            Update to v{node.latest_version}
          </Button>
          {loop && (
            <span id="node-update-refusal" className="text-muted-foreground text-xs">
              {loop === "Current workflow"
                ? "Can't update: this node runs this workflow itself."
                : `Can't update: v${node.latest_version} runs this workflow, which would make a loop.`}
            </span>
          )}
        </div>
      )}
      <div className="flex flex-col gap-1">
        <label htmlFor="node-connect" className="text-xs font-medium">
          Connect to
        </label>
        <div className="flex gap-2">
          <Select
            id="node-connect"
            className="min-w-0 flex-1"
            value={to}
            onChange={setTo}
            groups={[{ label: "Nodes", options: connectable }]}
            placeholder={connectable.length ? "Choose a node…" : "Nothing to connect to"}
          />
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
