import { describe, expect, it } from "vitest";

import { formFields, formValue } from "@/lib/schema-form";

const schema = {
  type: "object",
  properties: {
    topic: { type: "string", title: "Topic", description: "What it's about" },
    count: { type: "integer" },
    urgent: { type: "boolean" },
    tone: { enum: ["calm", "loud"] },
    extra: { type: "object" },
  },
  required: ["topic"],
};

describe("the start-run form", () => {
  it("has a field per property of an object schema", () => {
    expect(formFields(schema)).toEqual([
      {
        name: "topic",
        label: "Topic",
        kind: "text",
        required: true,
        description: "What it's about",
        options: undefined,
      },
      { name: "count", label: "count", kind: "integer", required: false, description: undefined, options: undefined },
      { name: "urgent", label: "urgent", kind: "boolean", required: false, description: undefined, options: undefined },
      { name: "tone", label: "tone", kind: "choice", required: false, description: undefined, options: ["calm", "loud"] },
      { name: "extra", label: "extra", kind: "json", required: false, description: undefined, options: undefined },
    ]);
  });

  it("is one JSON field for any other schema", () => {
    expect(formFields({ type: "array" })).toBeNull();
    expect(formFields(null)).toBeNull();
  });

  it("makes the input from what's typed, leaving blanks out", () => {
    const fields = formFields(schema)!;
    expect(
      formValue(fields, { topic: "tides", count: "3", urgent: "true", tone: '"calm"', extra: '{"a":1}' }),
    ).toEqual({ value: { topic: "tides", count: 3, urgent: true, tone: "calm", extra: { a: 1 } } });
    expect(formValue(fields, { topic: "tides", count: "" })).toEqual({ value: { topic: "tides" } });
    expect(formValue(fields, { count: "lots" })).toEqual({ error: "count: not a number" });
    expect(formValue(fields, { extra: "{" })).toEqual({ error: "extra: not valid JSON" });
  });
});
