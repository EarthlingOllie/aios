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
