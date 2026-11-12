/**
 * The start-run form for a workflow with an input schema: a field per property of an object
 * schema (text, a number, a yes/no, a choice, or JSON for anything else), or one JSON field for
 * a schema that isn't an object's. The backend checks what's sent against the schema, and
 * refuses the run with the errors if it doesn't fit.
 */
import type { JsonValue } from "@/lib/api";

export type FieldKind = "text" | "number" | "integer" | "boolean" | "choice" | "json";

export type FormField = {
  name: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  description?: string;
  /** For a choice: the values allowed. */
  options?: JsonValue[];
};

type Schema = { [key: string]: JsonValue };

const isObject = (v: unknown): v is Schema =>
  typeof v === "object" && v !== null && !Array.isArray(v);

function kindOf(schema: JsonValue): FieldKind {
  if (!isObject(schema)) return "json";
  if (Array.isArray(schema.enum)) return "choice";
  switch (schema.type) {
    case "string":
      return "text";
    case "number":
      return "number";
    case "integer":
      return "integer";
    case "boolean":
      return "boolean";
    default:
      return "json";
  }
}

/** The form's fields for an input schema; null if it isn't an object's (one JSON field then). */
export function formFields(schema: JsonValue | null | undefined): FormField[] | null {
  if (!isObject(schema) || schema.type !== "object" || !isObject(schema.properties)) return null;
  const required = Array.isArray(schema.required) ? schema.required : [];
  return Object.entries(schema.properties).map(([name, property]) => ({
    name,
    label: isObject(property) && typeof property.title === "string" ? property.title : name,
    kind: kindOf(property),
    required: required.includes(name),
    description:
      isObject(property) && typeof property.description === "string"
        ? property.description
        : undefined,
    options: isObject(property) && Array.isArray(property.enum) ? property.enum : undefined,
  }));
}

/** What the form's values (as typed, by field name) make: the run's input, or why they can't.
 * A field left blank is left out. */
export function formValue(
  fields: FormField[],
  values: Record<string, string>,
): { value: JsonValue } | { error: string } {
  const out: Record<string, JsonValue> = {};
  for (const field of fields) {
    const text = values[field.name] ?? "";
    if (text === "") continue;
    switch (field.kind) {
      case "text":
        out[field.name] = text;
        break;
      case "number":
      case "integer": {
        const n = Number(text);
        if (text.trim() === "" || Number.isNaN(n)) return { error: `${field.label}: not a number` };
        out[field.name] = n;
        break;
      }
      case "boolean":
        out[field.name] = text === "true";
        break;
      case "choice":
        out[field.name] = JSON.parse(text) as JsonValue;
        break;
      case "json":
        try {
          out[field.name] = JSON.parse(text) as JsonValue;
        } catch {
          return { error: `${field.label}: not valid JSON` };
        }
    }
  }
  return { value: out };
}
