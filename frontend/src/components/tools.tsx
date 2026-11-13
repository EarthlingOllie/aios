import { useEffect, useState } from "react";

import { Field } from "@/components/crud";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  api,
  ApiError,
  type AgentTool,
  type Connector,
  type ConnectorPin,
  type ConnectorTool,
  type SchemaProperty,
  type ToolInfo,
  type WorkflowSummary,
} from "@/lib/api";
import {
  connectorToolOptions,
  parseTargets,
  pinConnectorTargets,
  pinConnectorTools,
  targetsLabel,
  workflowOptions,
} from "@/lib/tools";

type Config = Record<string, unknown>;

/** Form fields for a tool's settings, generated from its JSON schema. */
export function ToolSettingsFields({
  tool,
  value,
  onChange,
}: {
  tool: ToolInfo;
  value: Config;
  onChange: (value: Config) => void;
}) {
  const schema = tool.config_schema.properties ?? {};
  const properties = Object.entries(schema);
  if (properties.length === 0) return null;
  return (
    <div className="flex flex-col gap-3">
      {properties.map(([name, prop]) => {
        const id = `tool-${tool.key}-${name}`;
        // Only the fields the tool has now: stored settings can carry ones it has since dropped,
        // which the server would reject alongside the edit.
        const set = (v: unknown) =>
          onChange({
            ...Object.fromEntries(Object.entries(value).filter(([k]) => k in schema)),
            [name]: v,
          });
        const current = value[name];
        let input: React.ReactNode;
        if (prop["x-picker"] === "workflows") {
          return (
            <WorkflowPicker
              key={name}
              prop={prop}
              value={Array.isArray(current) ? (current as number[]) : []}
              onChange={set}
            />
          );
        } else if (prop["x-picker"] === "connectors") {
          return (
            <ConnectorToolPicker
              key={name}
              prop={prop}
              value={Array.isArray(current) ? (current as ConnectorPin[]) : []}
              onChange={set}
            />
          );
        } else if (prop.type === "boolean") {
          input = (
            <input
              id={id}
              type="checkbox"
              checked={!!current}
              onChange={(e) => set(e.target.checked)}
            />
          );
        } else if (prop.type === "integer" || prop.type === "number") {
          input = (
            <Input
              id={id}
              type="number"
              className="w-28"
              min={prop.minimum}
              max={prop.maximum}
              step={prop.type === "integer" ? 1 : "any"}
              value={typeof current === "number" ? current : ""}
              // An empty field is sent as null, which the server rejects, rather than left out,
              // which it would quietly fill in with the default.
              onChange={(e) =>
                set(e.target.value === "" ? null : Number(e.target.value))
              }
            />
          );
        } else if (prop.type === "array" && prop.items?.type === "string") {
          // Split without trimming so typing round-trips; the server cleans up the entries.
          input = (
            <Input
              id={id}
              value={Array.isArray(current) ? current.join(",") : ""}
              onChange={(e) =>
                set(e.target.value === "" ? [] : e.target.value.split(","))
              }
              placeholder="Comma-separated"
            />
          );
        } else {
          input = (
            <Input
              id={id}
              value={typeof current === "string" ? current : ""}
              onChange={(e) => set(e.target.value)}
            />
          );
        }
        return (
          <Field
            key={name}
            label={prop.title ?? name}
            htmlFor={id}
            hint={prop.description}
          >
            {input}
          </Field>
        );
      })}
    </div>
  );
}

/** Picks workflows by id (`start_workflow`'s allowed workflows): the organisation's live
 * workflows, and any picked since archived or deleted, so they can be removed. */
function WorkflowPicker({
  prop,
  value,
  onChange,
}: {
  prop: SchemaProperty;
  value: number[];
  onChange: (value: number[]) => void;
}) {
  const [lists, setLists] = useState<[WorkflowSummary[], WorkflowSummary[]]>();
  const [error, setError] = useState<string>();
  useEffect(() => {
    let live = true;
    Promise.all([api.listWorkflows(), api.listWorkflows(true)])
      .then((found) => live && setLists(found))
      .catch(() => live && setError("Couldn't load the workflows."));
    return () => {
      live = false;
    };
  }, []);
  const options = lists ? workflowOptions(lists[0], lists[1], value) : [];
  const toggle = (id: number, on: boolean) =>
    onChange(on ? [...value.filter((v) => v !== id), id] : value.filter((v) => v !== id));
  return (
    <fieldset className="flex flex-col gap-1">
      <legend className="text-sm font-medium">{prop.title ?? "Workflows"}</legend>
      {prop.description && <p className="text-muted-foreground text-xs">{prop.description}</p>}
      {error && <p className="text-destructive text-xs">{error}</p>}
      {lists && options.length === 0 && (
        <p className="text-muted-foreground text-xs">There are no workflows yet.</p>
      )}
      {options.map((option) => (
        <label key={option.id} className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={option.checked}
            onChange={(e) => toggle(option.id, e.target.checked)}
          />
          <span>{option.name}</span>
          {option.note && <Badge variant="secondary">{option.note}</Badge>}
        </label>
      ))}
    </fieldset>
  );
}

/** Picks connectors' tools by name (the `connector` tool's settings): each of the
 * organisation's connectors is a group of the tools it offers now, and connectors or tools picked
 * since removed stay listed, saying so, so they can be removed. */
function ConnectorToolPicker({
  prop,
  value,
  onChange,
}: {
  prop: SchemaProperty;
  value: ConnectorPin[];
  onChange: (value: ConnectorPin[]) => void;
}) {
  const [connectors, setConnectors] = useState<Connector[]>();
  const [error, setError] = useState<string>();
  useEffect(() => {
    let live = true;
    api
      .listConnectors()
      .then((found) => live && setConnectors(found))
      .catch(() => live && setError("Couldn't load the connectors."));
    return () => {
      live = false;
    };
  }, []);
  const shown = (connectors ?? []).filter((c) => c.status !== "removing");
  const known = new Set(shown.map((c) => c.id));
  const removed = connectors ? value.filter((p) => !known.has(p.connector_id)) : [];
  return (
    <fieldset className="flex flex-col gap-3" data-testid="connector-tools">
      <legend className="text-sm font-medium">{prop.title ?? "Connectors"}</legend>
      {prop.description && <p className="text-muted-foreground text-xs">{prop.description}</p>}
      {error && <p className="text-destructive text-xs">{error}</p>}
      {connectors && shown.length === 0 && removed.length === 0 && (
        <p className="text-muted-foreground text-xs">
          There are no connectors yet: an admin connects them on the Sources tab.
        </p>
      )}
      {shown.map((connector) => (
        <ConnectorGroup
          key={connector.id}
          connector={connector}
          pinned={value.find((p) => p.connector_id === connector.id)?.tools ?? []}
          targets={value.find((p) => p.connector_id === connector.id)?.targets ?? []}
          onChange={(tools) => onChange(pinConnectorTools(value, connector, tools))}
          onTargets={(targets) => onChange(pinConnectorTargets(value, connector.id, targets))}
        />
      ))}
      {removed.map((pin) => (
        <div key={pin.connector_id} className="flex flex-col gap-1" data-testid="connector-group">
          <span className="flex items-center gap-2 text-sm font-medium">
            {pin.provider} connector <Badge variant="secondary">removed</Badge>
          </span>
          <button
            type="button"
            className="text-muted-foreground self-start text-xs underline"
            onClick={() =>
              onChange(pinConnectorTools(value, { id: pin.connector_id, provider: pin.provider }, []))
            }
          >
            Remove its tools
          </button>
        </div>
      ))}
    </fieldset>
  );
}

/** One connector's tools, as checkboxes, and, once any is picked, what they may act on. */
function ConnectorGroup({
  connector,
  pinned,
  targets,
  onChange,
  onTargets,
}: {
  connector: Connector;
  pinned: string[];
  targets: string[];
  onChange: (tools: string[]) => void;
  onTargets: (targets: string[]) => void;
}) {
  const [offered, setOffered] = useState<ConnectorTool[]>();
  const [error, setError] = useState<string>();
  const [typed, setTyped] = useState(() => targets.join(", "));
  useEffect(() => {
    let live = true;
    api
      .connectorTools(connector.id)
      .then((found) => live && setOffered(found))
      .catch(
        (e) =>
          live &&
          setError(e instanceof ApiError ? e.message : "Couldn't load this connector's tools."),
      );
    return () => {
      live = false;
    };
  }, [connector.id]);
  const options = connectorToolOptions(error ? undefined : offered, pinned);
  const toggle = (name: string, on: boolean) =>
    onChange(on ? [...pinned.filter((n) => n !== name), name] : pinned.filter((n) => n !== name));
  return (
    <div className="flex flex-col gap-1" data-testid="connector-group">
      <span className="text-sm font-medium">{connector.name}</span>
      {error && <p className="text-destructive text-xs">{error}</p>}
      {!error && !offered && <p className="text-muted-foreground text-xs">Loading its tools…</p>}
      {offered && !error && options.length === 0 && (
        <p className="text-muted-foreground text-xs">It offers no tools.</p>
      )}
      {options.map((option) => (
        <label key={option.name} className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-1"
            checked={option.checked}
            onChange={(e) => toggle(option.name, e.target.checked)}
          />
          <span className="flex flex-col">
            <span className="flex items-center gap-2">
              <span className="font-mono text-xs">{option.name}</span>
              {option.readOnly && <Badge variant="outline">read-only</Badge>}
              {option.gone && <Badge variant="secondary">no longer offered</Badge>}
            </span>
            {option.description && (
              <span className="text-muted-foreground text-xs">{option.description}</span>
            )}
          </span>
        </label>
      ))}
      {pinned.length > 0 && (
        <label className="mt-1 flex flex-col gap-1 text-sm">
          <span>{targetsLabel(connector.provider)}</span>
          <Input
            value={typed}
            placeholder={connector.provider === "github" ? "owner/repository, …" : "Any"}
            onChange={(e) => {
              setTyped(e.target.value);
              onTargets(parseTargets(e.target.value));
            }}
          />
          <span className="text-muted-foreground text-xs">
            {connector.provider === "github"
              ? "Its tools act only in these repositories (owner/name, separated by commas): with none, they can't act at all."
              : "Separated by commas. Left empty, its tools may act on anything the connector reaches."}
          </span>
        </label>
      )}
    </div>
  );
}

function formatSetting(value: unknown): string | undefined {
  if (Array.isArray(value) && value.some((v) => v && typeof v === "object" && "tools" in v)) {
    const pins = value as ConnectorPin[];
    const on = (p: ConnectorPin) => (p.targets?.length ? ` (on ${p.targets.join(", ")})` : "");
    return pins.map((p) => `${p.provider}: ${p.tools.join(", ")}${on(p)}`).join("; ") || undefined;
  }
  if (Array.isArray(value)) return value.length ? value.join(", ") : undefined;
  if (value === null || value === undefined || value === "") return undefined;
  return String(value);
}

/** An attached tool and a one-line summary of its settings. */
export function ToolRow({ tool, info }: { tool: AgentTool; info?: ToolInfo }) {
  const properties = info?.config_schema.properties ?? {};
  const settings = Object.entries(tool.config)
    .map(([name, value]) => {
      const formatted = formatSetting(value);
      return formatted && `${properties[name]?.title ?? name}: ${formatted}`;
    })
    .filter(Boolean)
    .join(" · ");
  return (
    <li className="px-3 py-2">
      <span className="flex items-center gap-2">
        <span className="text-sm font-medium">{tool.name}</span>
        {!tool.available && (
          <Badge variant="secondary">no longer available</Badge>
        )}
      </span>
      {tool.description && (
        <span className="text-muted-foreground block truncate text-xs">
          {tool.description}
        </span>
      )}
      {settings && (
        <span className="text-muted-foreground block text-xs">{settings}</span>
      )}
    </li>
  );
}

export function ToolList({
  tools,
  catalog,
}: {
  tools: AgentTool[];
  catalog?: ToolInfo[];
}) {
  if (tools.length === 0)
    return <p className="text-muted-foreground text-sm">No tools.</p>;
  return (
    <ul className="divide-y rounded-md border">
      {tools.map((tool) => (
        <ToolRow
          key={tool.key}
          tool={tool}
          info={catalog?.find((t) => t.key === tool.key)}
        />
      ))}
    </ul>
  );
}

/** The tools added, removed or reconfigured between two versions. */
export function ToolChanges({ before, after }: { before: AgentTool[]; after: AgentTool[] }) {
  const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
  const changes = [
    ...after
      .filter((t) => !before.some((b) => b.key === t.key))
      .map((t) => `Added ${t.name}`),
    ...before
      .filter((t) => !after.some((a) => a.key === t.key))
      .map((t) => `Removed ${t.name}`),
    ...after
      .filter((t) => {
        const old = before.find((b) => b.key === t.key);
        return old && !same(old.config, t.config);
      })
      .map((t) => `Changed the settings of ${t.name}`),
  ];
  if (changes.length === 0)
    return <p className="text-muted-foreground text-sm">No differences.</p>;
  return (
    <ul className="list-disc pl-5 text-sm">
      {changes.map((change) => (
        <li key={change}>{change}</li>
      ))}
    </ul>
  );
}
