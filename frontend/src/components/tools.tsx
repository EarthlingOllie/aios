import { useEffect, useState } from "react";

import { Field } from "@/components/crud";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { api, type AgentTool, type SchemaProperty, type ToolInfo, type WorkflowSummary } from "@/lib/api";
import { workflowOptions } from "@/lib/tools";

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

function formatSetting(value: unknown): string | undefined {
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
