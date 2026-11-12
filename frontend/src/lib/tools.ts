import type { ConnectorPin, ConnectorTool } from "@/lib/api";

/** A workflow as a tool's workflow picker offers it (`start_workflow`'s allowed workflows). */
export type WorkflowOption = {
  id: number;
  name: string;
  checked: boolean;
  /** Why a picked workflow can no longer be started: "archived" or "deleted". */
  note: "archived" | "deleted" | null;
};

type Named = { id: number; name: string };

/**
 * What a workflow picker offers: the workflows picked first (the live ones in list order, then
 * any since archived, then any since deleted, each saying so, so they can be removed), then
 * the live ones that aren't. Archived workflows that aren't picked aren't offered.
 */
export function workflowOptions(live: Named[], archived: Named[], picked: number[]): WorkflowOption[] {
  const chosen = new Set(picked);
  const known = new Set([...live, ...archived].map((w) => w.id));
  return [
    ...live
      .filter((w) => chosen.has(w.id))
      .map((w) => ({ id: w.id, name: w.name, checked: true, note: null })),
    ...archived
      .filter((w) => chosen.has(w.id))
      .map((w) => ({ id: w.id, name: w.name, checked: true, note: "archived" as const })),
    ...[...chosen]
      .filter((id) => !known.has(id))
      .map((id) => ({ id, name: `Workflow ${id}`, checked: true, note: "deleted" as const })),
    ...live
      .filter((w) => !chosen.has(w.id))
      .map((w) => ({ id: w.id, name: w.name, checked: false, note: null })),
  ];
}

/** One of a connector's tools as the agent editor's picker offers it. */
export type ConnectorToolOption = {
  name: string;
  description: string;
  readOnly: boolean;
  checked: boolean;
  /** Pinned, but the connector no longer offers it: left out of turns until it's removed. */
  gone: boolean;
};

/**
 * What a connector's group in the tool picker offers: the pinned tools first (in the
 * connector's order, then any it no longer offers, saying so, so they can be removed), then the
 * rest. While the connector's tools can't be listed (`offered` undefined), only the pinned ones.
 */
export function connectorToolOptions(
  offered: ConnectorTool[] | undefined,
  pinned: string[],
): ConnectorToolOption[] {
  const chosen = new Set(pinned);
  const option = (t: ConnectorTool, checked: boolean): ConnectorToolOption => ({
    name: t.name,
    description: t.description,
    readOnly: t.read_only,
    checked,
    gone: false,
  });
  if (offered === undefined)
    return pinned.map((name) => ({ name, description: "", readOnly: false, checked: true, gone: false }));
  const known = new Set(offered.map((t) => t.name));
  return [
    ...offered.filter((t) => chosen.has(t.name)).map((t) => option(t, true)),
    ...pinned
      .filter((name) => !known.has(name))
      .map((name) => ({ name, description: "", readOnly: false, checked: true, gone: true })),
    ...offered.filter((t) => !chosen.has(t.name)).map((t) => option(t, false)),
  ];
}

/** The `connector` tool's settings with `connector`'s group set to `tools`: added if it's new,
 * dropped if it's left with none. */
export function pinConnectorTools(
  pins: ConnectorPin[],
  connector: { id: number; provider: string },
  tools: string[],
): ConnectorPin[] {
  const group = { connector_id: connector.id, provider: connector.provider, tools };
  const at = pins.findIndex((p) => p.connector_id === connector.id);
  if (at < 0) return tools.length ? [...pins, group] : pins;
  return tools.length
    ? pins.map((p, i) => (i === at ? group : p))
    : pins.filter((_, i) => i !== at);
}
