import type { InvocableBy, LimitName, Limits } from "@/lib/api";

/** The caps on a turn (an agent's) and on a workflow run (a workflow's), in the order shown. */
export const TURN_LIMITS = ["turn_model_calls", "turn_tokens", "turn_cost"] as const;
export const RUN_LIMITS = ["run_model_calls", "run_cost"] as const;
export const ALL_LIMITS: readonly LimitName[] = [...TURN_LIMITS, ...RUN_LIMITS];

export const LIMIT_LABELS: Record<LimitName, string> = {
  turn_model_calls: "Model calls per turn",
  turn_tokens: "Tokens per turn",
  turn_cost: "Cost per turn (£)",
  run_model_calls: "Model calls per workflow run",
  run_cost: "Cost per workflow run (£)",
};

const isCost = (name: LimitName) => name.endsWith("_cost");

/** A cap's value as shown: pounds for a cost, a whole number otherwise. */
export function formatLimit(name: LimitName, value: number): string {
  return isCost(name) ? `£${value.toFixed(2)}` : value.toLocaleString("en-GB");
}

/** An estimated cost in pounds: to the penny, or to four places when under a penny. */
export function formatCost(cost: number | null): string {
  if (cost === null) return "No price";
  if (cost > 0 && cost < 0.01) return `£${cost.toFixed(4)}`;
  return `£${cost.toFixed(2)}`;
}

/** Caps as a form holds them: what's typed, blank following the organisation's. */
export type LimitsDraft<N extends LimitName> = Record<N, string>;

export function draftOf<N extends LimitName>(names: readonly N[], limits: Partial<Limits>): LimitsDraft<N> {
  return Object.fromEntries(names.map((n) => [n, limits[n] == null ? "" : String(limits[n])])) as LimitsDraft<N>;
}

/** The caps a draft sets (null where blank); undefined if any isn't valid: a count must be a
 * whole number, and every cap above zero. */
export function limitsOf<N extends LimitName>(
  names: readonly N[],
  draft: LimitsDraft<N>,
): Record<N, number | null> | undefined {
  const out = {} as Record<N, number | null>;
  for (const name of names) {
    const text = draft[name].trim();
    if (!text) {
      out[name] = null;
      continue;
    }
    const value = Number(text);
    if (!Number.isFinite(value) || value <= 0 || (!isCost(name) && !Number.isInteger(value))) return undefined;
    out[name] = value;
  }
  return out;
}

/** The caps in `limits` set above the organisation's (`ceiling`), which only admins may do,
 * leaving out those already at that value (`saved`): an admin set them. */
export function aboveCeiling(
  limits: Partial<Record<LimitName, number | null>>,
  ceiling: Limits,
  saved: Partial<Record<LimitName, number | null>> = {},
): LimitName[] {
  return ALL_LIMITS.filter((name) => {
    const value = limits[name];
    const limit = ceiling[name];
    return value != null && limit != null && value > limit && saved[name] !== value;
  });
}

export const INVOCABLE_BY: InvocableBy[] = ["everyone", "editors", "admins"];

const INVOCABLE_BY_LABELS: Record<InvocableBy, string> = {
  everyone: "Everyone",
  editors: "Editors and admins",
  admins: "Admins only",
};

export function invocableByLabel(value: InvocableBy): string {
  return INVOCABLE_BY_LABELS[value];
}
