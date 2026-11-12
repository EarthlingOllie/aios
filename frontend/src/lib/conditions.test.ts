import { describe, expect, it } from "vitest";

import {
  describeCondition,
  fromCondition,
  parseJson,
  parseValue,
  showJson,
  toCondition,
} from "@/lib/conditions";

describe("the condition builder", () => {
  it("makes a JSONLogic comparison of a field with a value", () => {
    expect(toCondition({ field: "output.score", op: ">", value: 5 })).toEqual({
      ">": [{ var: "output.score" }, 5],
    });
  });

  it("reads back the comparisons it makes", () => {
    const made = toCondition({ field: "input.region", op: "==", value: "EU" });
    expect(fromCondition(made)).toEqual({ field: "input.region", op: "==", value: "EU" });
  });

  it("leaves anything else to the raw editor", () => {
    expect(fromCondition(null)).toBeNull();
    expect(fromCondition({ and: [true, true] })).toBeNull();
    expect(fromCondition({ ">": [{ var: "output.a" }, { var: "output.b" }] })).toBeNull();
    expect(fromCondition({ ">": [1, 2, 3] })).toBeNull();
    expect(fromCondition({ "<": [1, { var: "output.a" }] })).toBeNull();
  });

  it("reads typed values as numbers, booleans and JSON where they are", () => {
    expect(parseValue("5")).toBe(5);
    expect(parseValue("-2.5")).toBe(-2.5);
    expect(parseValue("true")).toBe(true);
    expect(parseValue("null")).toBeNull();
    expect(parseValue('["a","b"]')).toEqual(["a", "b"]);
    expect(parseValue("high")).toBe("high");
    expect(parseValue("[not json")).toBe("[not json");
  });

  it("labels edges by their condition", () => {
    expect(describeCondition(undefined)).toBeNull();
    expect(describeCondition({ ">": [{ var: "output.score" }, 5] })).toBe("if score > 5");
    expect(describeCondition({ or: [] })).toBe("if …");
  });
});

describe("raw JSON fields", () => {
  it("parse JSON, blank as nothing, and say why when it isn't", () => {
    expect(parseJson("")).toEqual({ value: null });
    expect(parseJson('{"type": "object"}')).toEqual({ value: { type: "object" } });
    expect(parseJson("{")).toHaveProperty("error");
    expect(showJson(null)).toBe("");
    expect(showJson({ a: 1 })).toBe('{\n  "a": 1\n}');
  });
});
