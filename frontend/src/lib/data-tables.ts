import type { DataTable, TableAccess, TableAgent } from "@/lib/api";

/** Rows of a table shown at once. */
export const PAGE_SIZE = 50;

/** How a table's row count reads: "3 rows", "About 1,234,567 rows", or null if unknown. */
export function rowCountLabel(table: Pick<DataTable, "row_count" | "row_count_estimated">) {
  if (table.row_count === null) return null;
  const n = table.row_count;
  const count = `${n.toLocaleString("en-US")} ${n === 1 ? "row" : "rows"}`;
  return table.row_count_estimated ? `About ${count}` : count;
}

export type Sort = { column: string; order: "asc" | "desc" } | null;

/** Clicking a column's header: sorts by it ascending, then descending, then as stored. */
export function nextSort(current: Sort, column: string): Sort {
  if (current?.column !== column) return { column, order: "asc" };
  return current.order === "asc" ? { column, order: "desc" } : null;
}

/** "Rows 51–100", for the rows from `offset` on a page of `count` of them; null if none. */
export function rowRangeLabel(offset: number, count: number): string | null {
  if (count === 0) return null;
  const first = (offset + 1).toLocaleString("en-US");
  const last = (offset + count).toLocaleString("en-US");
  return count === 1 ? `Row ${first}` : `Rows ${first}–${last}`;
}

/** Who may write a table, in a few words: its owner, "No owner" (any agent may), or read-only
 * while its owner is archived. */
export function accessLabel(table: Pick<TableAccess, "owner" | "shares">): string {
  const { owner } = table;
  if (owner === null) return "No owner";
  if (owner.archived) return `Read-only: its owner, ${owner.name}, is archived`;
  return `Owned by ${owner.name}`;
}

/** The agents a table can be given to: any that isn't archived, but its owner. */
export function ownerCandidates<A extends TableAgent>(agents: A[], table: Pick<TableAccess, "owner">): A[] {
  return agents.filter((a) => !a.archived && a.id !== table.owner?.id);
}

/** The agents a table can be shared with: any that isn't archived, owns it or has it already;
 * none while it has no owner (any agent may write it then). */
export function shareCandidates<A extends TableAgent>(
  agents: A[],
  table: Pick<TableAccess, "owner" | "shares">,
): A[] {
  if (table.owner === null) return [];
  const shared = new Set(table.shares.map((a) => a.id));
  return ownerCandidates(agents, table).filter((a) => !shared.has(a.id));
}
