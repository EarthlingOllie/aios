/**
 * Structured workflows in the editor and on runs: fields for schemas, input templates and edge
 * conditions (a simple builder, with a raw editor for anything more), the start-run form made
 * from the input schema, and structured values shown formatted.
 */
import { useId, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { JsonValue } from "@/lib/api";
import {
  fromCondition,
  OPERATORS,
  operatorLabel,
  parseJson,
  parseValue,
  showJson,
  showValue,
  toCondition,
  type Operator,
} from "@/lib/conditions";
import { formFields, formValue } from "@/lib/schema-form";

/**
 * A JSON value edited as text: committed (`onChange`) when the field loses focus, if it parses;
 * blank is none (null). `label` names it; `hint` says what it's for.
 */
export function JsonField({
  label,
  value,
  onChange,
  hint,
  placeholder,
  rows = 4,
}: {
  label: string;
  value: JsonValue | null | undefined;
  onChange: (value: JsonValue | null) => void;
  hint?: ReactNode;
  placeholder?: string;
  rows?: number;
}) {
  const id = useId();
  const [text, setText] = useState(() => showJson(value));
  const [error, setError] = useState<string | null>(null);
  const commit = () => {
    const parsed = parseJson(text);
    if ("error" in parsed) return setError(parsed.error);
    setError(null);
    onChange(parsed.value);
  };
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium">
        {label}
      </label>
      <Textarea
        id={id}
        rows={rows}
        className="font-mono text-xs"
        value={text}
        placeholder={placeholder}
        aria-invalid={!!error}
        onChange={(e) => {
          setText(e.target.value);
          // So the form won't submit, dropping the edit, while it isn't JSON.
          const parsed = parseJson(e.target.value);
          e.target.setCustomValidity("error" in parsed ? parsed.error : "");
        }}
        onBlur={commit}
      />
      {hint && <span className="text-muted-foreground text-xs">{hint}</span>}
      {error && <span className="text-destructive text-xs">{error}</span>}
    </div>
  );
}

/** A template as typed: JSON if it reads as an object or a list, otherwise the text itself. */
function parseTemplate(text: string): JsonValue | null {
  if (!text.trim()) return null;
  if (/^\s*[[{]/.test(text)) {
    try {
      return JSON.parse(text) as JsonValue;
    } catch {
      // the text
    }
  }
  return text;
}

const showTemplate = (template: JsonValue | null | undefined) =>
  typeof template === "string" ? template : showJson(template);

/** A node's input template: text with placeholders, or JSON whose strings have them. */
export function TemplateField({
  value,
  onChange,
  predecessors,
}: {
  value: JsonValue | null | undefined;
  onChange: (value: JsonValue | null) => void;
  predecessors: string[];
}) {
  const id = useId();
  const [text, setText] = useState(() => showTemplate(value));
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium">
        Input template
      </label>
      <Textarea
        id={id}
        rows={3}
        className="font-mono text-xs"
        value={text}
        placeholder="Blank: its predecessors' outputs, as without one"
        onChange={(e) => setText(e.target.value)}
        onBlur={() => onChange(parseTemplate(text))}
      />
      <span className="text-muted-foreground text-xs">
        Placeholders only: {"{{input}}"}, {"{{input.field}}"}
        {predecessors.length > 0 &&
          `, ${predecessors.map((p) => `{{nodes.${p}}}`).join(", ")} or a field of one`}
        . A missing field fails the step.
      </span>
    </div>
  );
}

/** An edge's condition: the builder for one field compared with a value, or the raw editor. */
export function ConditionEditor({
  value,
  onChange,
}: {
  value: JsonValue | null | undefined;
  onChange: (value: JsonValue | null) => void;
}) {
  const simple = value === null || value === undefined ? null : fromCondition(value);
  const [raw, setRaw] = useState(value !== null && value !== undefined && !simple);
  const fieldId = useId();
  const opId = useId();
  const valueId = useId();
  const [field, setField] = useState(simple?.field ?? "output.");
  const [op, setOp] = useState<Operator>(simple?.op ?? "==");
  const [typed, setTyped] = useState(simple ? showValue(simple.value) : "");
  const apply = (next: { field?: string; op?: Operator; typed?: string }) => {
    const f = next.field ?? field;
    const o = next.op ?? op;
    const t = next.typed ?? typed;
    if (f.trim() && f !== "output." && t !== "") {
      onChange(toCondition({ field: f.trim(), op: o, value: parseValue(t) }));
    }
  };
  return (
    <div className="flex flex-col gap-2 border-t pt-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium">Condition</span>
        <Button type="button" size="sm" variant="ghost" onClick={() => setRaw(!raw)}>
          {raw ? "Use the builder" : "Edit as JSON"}
        </Button>
      </div>
      {raw ? (
        <JsonField
          label="Condition (JSONLogic)"
          value={value}
          onChange={onChange}
          rows={5}
          hint="Reads output (the source's output) and input (the run's input). Blank: always taken."
        />
      ) : (
        <div className="flex flex-col gap-2">
          <label htmlFor={fieldId} className="sr-only">
            Field
          </label>
          <Input
            id={fieldId}
            className="font-mono text-xs"
            value={field}
            placeholder="output.score"
            onChange={(e) => setField(e.target.value)}
            onBlur={() => apply({})}
          />
          <label htmlFor={opId} className="sr-only">
            Comparison
          </label>
          <select
            id={opId}
            className="border-input bg-background h-8 rounded-md border px-2 text-sm"
            value={op}
            onChange={(e) => {
              setOp(e.target.value as Operator);
              apply({ op: e.target.value as Operator });
            }}
          >
            {OPERATORS.map((o) => (
              <option key={o} value={o}>
                {operatorLabel[o]}
              </option>
            ))}
          </select>
          <label htmlFor={valueId} className="sr-only">
            Value
          </label>
          <Input
            id={valueId}
            className="font-mono text-xs"
            value={typed}
            placeholder="5"
            onChange={(e) => setTyped(e.target.value)}
            onBlur={() => apply({})}
          />
          <span className="text-muted-foreground text-xs">
            The edge is taken only when this holds. Nodes no taken edge reaches are skipped.
          </span>
        </div>
      )}
      {value !== null && value !== undefined && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => {
            setField("output.");
            setTyped("");
            onChange(null);
          }}
        >
          Remove condition
        </Button>
      )}
    </div>
  );
}

/** A structured value, formatted: JSON pretty-printed, text as it is. */
export function formatValue(value: JsonValue | null | undefined, text: string | null): string {
  if (value !== null && value !== undefined && typeof value !== "string") {
    return JSON.stringify(value, null, 2);
  }
  return text ?? "";
}

/**
 * The start-run form's fields for a workflow with an input schema: one per property of an
 * object schema, or one JSON field otherwise. `onSubmit` gets the input, or an error to show.
 */
export function SchemaInputForm({
  schema,
  submitting,
  onSubmit,
  onCancel,
}: {
  schema: JsonValue;
  submitting: boolean;
  onSubmit: (input: JsonValue) => void;
  onCancel: () => void;
}) {
  const fields = formFields(schema);
  const [values, setValues] = useState<Record<string, string>>({});
  const [json, setJson] = useState("");
  const [error, setError] = useState<string | null>(null);
  const idBase = useId();
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (fields) {
      const made = formValue(fields, values);
      if ("error" in made) return setError(made.error);
      setError(null);
      return onSubmit(made.value);
    }
    const parsed = parseJson(json);
    if ("error" in parsed) return setError(parsed.error);
    setError(null);
    onSubmit(parsed.value);
  };
  const set = (name: string, value: string) => setValues({ ...values, [name]: value });
  return (
    <form
      className="flex flex-col gap-3 rounded-md border p-4"
      onSubmit={submit}
      aria-label="Start a run"
    >
      <p className="text-sm font-medium">Input</p>
      {fields ? (
        fields.map((f) => {
          const id = `${idBase}-${f.name}`;
          return (
            <div key={f.name} className="flex flex-col gap-1">
              <label htmlFor={id} className="text-sm">
                {f.label}
                {f.required && <span className="text-destructive"> *</span>}
              </label>
              {f.kind === "boolean" ? (
                <select
                  id={id}
                  className="border-input bg-background h-8 rounded-md border px-2 text-sm"
                  value={values[f.name] ?? ""}
                  onChange={(e) => set(f.name, e.target.value)}
                >
                  <option value="">—</option>
                  <option value="true">Yes</option>
                  <option value="false">No</option>
                </select>
              ) : f.kind === "choice" ? (
                <select
                  id={id}
                  className="border-input bg-background h-8 rounded-md border px-2 text-sm"
                  value={values[f.name] ?? ""}
                  onChange={(e) => set(f.name, e.target.value)}
                >
                  <option value="">—</option>
                  {f.options!.map((o) => (
                    <option key={JSON.stringify(o)} value={JSON.stringify(o)}>
                      {showValue(o)}
                    </option>
                  ))}
                </select>
              ) : f.kind === "json" ? (
                <Textarea
                  id={id}
                  rows={2}
                  className="font-mono text-xs"
                  value={values[f.name] ?? ""}
                  onChange={(e) => set(f.name, e.target.value)}
                />
              ) : (
                <Input
                  id={id}
                  type={f.kind === "text" ? "text" : "number"}
                  step={f.kind === "integer" ? 1 : "any"}
                  required={f.required}
                  value={values[f.name] ?? ""}
                  onChange={(e) => set(f.name, e.target.value)}
                />
              )}
              {f.description && (
                <span className="text-muted-foreground text-xs">{f.description}</span>
              )}
            </div>
          );
        })
      ) : (
        <JsonInput value={json} onChange={setJson} />
      )}
      <p className="text-muted-foreground text-xs">
        Checked against the workflow's input schema before the run starts.
      </p>
      {error && (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={submitting}>
          Start run
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function JsonInput({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm">
        Input (JSON)
      </label>
      <Textarea
        id={id}
        rows={4}
        className="font-mono text-xs"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}
