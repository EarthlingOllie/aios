import type { ImportedItem, ImportResult } from "@/lib/api";

const KIND = { skill: "Skill", agent: "Agent", workflow: "Workflow" } as const;

export type ImportLine = {
  key: string;
  /** What is (or would be) made, by the name it's made with. */
  text: string;
  /** Why the name differs from the file's, if it does. */
  note: string | null;
  /** Why it can't run here, if it can't. */
  warning: string | null;
};

/** An import's (or a dry run's) items as lines to list: what's made, renamed and can't run. */
export function importLines(items: ImportedItem[]): ImportLine[] {
  return items.map((i) => ({
    key: i.ref,
    text: `${KIND[i.kind]} ${i.imported_name}`,
    note: i.renamed ? `renamed from ${i.name}: that name is taken` : null,
    warning: i.unrunnable,
  }));
}

/** The page of the agent or workflow an import made; null for a dry run, which made nothing. */
export function importedPath(result: ImportResult): string | null {
  const { root } = result;
  if (result.dry_run || root.id === null) return null;
  return `/${root.kind}s/${root.id}`;
}
