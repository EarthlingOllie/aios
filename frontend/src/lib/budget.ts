/** Budgets: the organisation's monthly budget and allowances, in estimated pounds sterling per
 * calendar month in UTC. */

/** A sum of pounds, to the penny, with thousands separated. */
export function formatPounds(amount: number): string {
  return `£${amount.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** What an amount field holds: null for blank (none), the pounds typed, or undefined for
 * something that isn't a sum of pounds. */
export function amountOf(text: string): number | null | undefined {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value < 0) return undefined;
  return value;
}

export type BudgetLevel = "none" | "ok" | "warning" | "reached";

/** How spending stands against a budget: its share of it, and whether it's past the warning
 * threshold or has reached it (work is then refused). */
export function budgetState(
  spent: number,
  budget: number | null,
  threshold: number,
): { share: number | null; level: BudgetLevel } {
  if (budget === null) return { share: null, level: "none" };
  const share = budget > 0 ? spent / budget : 1;
  if (spent >= budget) return { share, level: "reached" };
  return { share, level: share >= threshold ? "warning" : "ok" };
}

/** A month (`YYYY-MM`) as it reads: "November 2026". */
export function monthLabel(month: string): string {
  const [year, number] = month.split("-").map(Number);
  return new Date(Date.UTC(year, number - 1, 1)).toLocaleDateString("en-GB", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}
