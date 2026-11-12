/**
 * Conditions on workflow edges, as the editor's simple builder makes them: one field of the
 * source's output (or the run's input) compared with a value. They're JSONLogic, a fixed subset
 * the backend evaluates (`aios.workflows.structured`); anything more is written in the raw
 * editor. A condition the builder can't show is edited raw.
 */
import type { JsonValue } from "@/lib/api";

export const OPERATORS = ["==", "!=", ">", ">=", "<", "<=", "in"] as const;
export type Operator = (typeof OPERATORS)[number];

export const operatorLabel: Record<Operator, string> = {
  "==": "equals",
  "!=": "doesn't equal",
  ">": "is more than",
  ">=": "is at least",
  "<": "is less than",
  "<=": "is at most",
  in: "is one of / is in",
};

/** A comparison of one field with a value: `field` is a path such as "output.score". */
export type Simple = { field: string; op: Operator; value: JsonValue };

/** The condition for a comparison. */
export function toCondition({ field, op, value }: Simple): JsonValue {
  return { [op]: [{ var: field }, value] };
}

const isObject = (v: unknown): v is Record<string, JsonValue> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** The comparison a condition is, if the builder can show it; null if it needs the raw editor. */
export function fromCondition(condition: JsonValue | null | undefined): Simple | null {
  if (!isObject(condition)) return null;
  const entries = Object.entries(condition);
  if (entries.length !== 1) return null;
  const [op, args] = entries[0];
  if (!(OPERATORS as readonly string[]).includes(op) || !Array.isArray(args) || args.length !== 2) {
    return null;
  }
  const [left, value] = args;
  if (!isObject(left) || Object.keys(left).length !== 1 || typeof left.var !== "string") return null;
  if (isObject(value) && Object.keys(value).length === 1) return null; // an operation, not a value
  return { field: left.var, op: op as Operator, value };
}

/** A value typed into the builder: a number or true/false/null if it reads as one, JSON if it
 * starts like JSON, else the text itself. */
export function parseValue(text: string): JsonValue {
  const trimmed = text.trim();
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed);
  if (trimmed === "true" || trimmed === "false") return trimmed === "true";
  if (trimmed === "null") return null;
  if (/^[[{"]/.test(trimmed)) {
    try {
      return JSON.parse(trimmed) as JsonValue;
    } catch {
      // not JSON: the text
    }
  }
  return text;
}

/** A value as the builder shows it: text as itself, anything else as JSON. */
export function showValue(value: JsonValue): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** A short reading of a condition, for an edge's label: "score > 5", or "condition". */
export function describeCondition(condition: JsonValue | null | undefined): string | null {
  if (condition === null || condition === undefined) return null;
  const simple = fromCondition(condition);
  if (!simple) return "if …";
  const field = simple.field.replace(/^output\./, "");
  return `if ${field} ${simple.op} ${JSON.stringify(simple.value)}`;
}

/** Parses JSON typed into a raw editor: the value, or why it isn't JSON. Blank is null. */
export function parseJson(text: string): { value: JsonValue | null } | { error: string } {
  if (!text.trim()) return { value: null };
  try {
    return { value: JSON.parse(text) as JsonValue };
  } catch (err) {
    return { error: `Not valid JSON: ${(err as Error).message}` };
  }
}

/** JSON for a raw editor: pretty, or blank for nothing. */
export function showJson(value: JsonValue | null | undefined): string {
  return value === null || value === undefined ? "" : JSON.stringify(value, null, 2);
}
