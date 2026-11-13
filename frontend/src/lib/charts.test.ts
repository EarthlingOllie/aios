import { describe, expect, it } from "vitest";

import { chartTheme, drawnSpec, truncationNote } from "./charts";

describe("drawing a chart", () => {
  const spec = { mark: "bar", encoding: { x: { field: "name", type: "nominal" } } };
  const rows = [{ name: "pike" }, { name: "carp" }];

  it("supplies the query's rows inline, and fills the width it's given", () => {
    expect(drawnSpec(spec, rows)).toEqual({
      ...spec,
      data: { values: rows },
      width: "container",
      autosize: { type: "fit", contains: "padding" },
    });
  });

  it("never keeps data or a schema of the spec's own", () => {
    const drawn = drawnSpec({ ...spec, data: { url: "https://example.com/x.csv" }, $schema: "x" }, rows);
    expect(drawn.data).toEqual({ values: rows });
    expect(drawn).not.toHaveProperty("$schema");
  });

  it("keeps a width the spec sets", () => {
    expect(drawnSpec({ ...spec, width: 300 }, rows).width).toBe(300);
  });
});

describe("the chart theme", () => {
  it("draws text and grid lines in the app's colours for light and dark", () => {
    const light = chartTheme("light");
    const dark = chartTheme("dark");
    expect(light.background).toBe("transparent");
    expect(light.axis.labelColor).not.toBe(dark.axis.labelColor);
    expect(light.axis.gridColor).not.toBe(dark.axis.gridColor);
    expect(dark.title.color).toBe(dark.axis.titleColor);
    expect(light.range.category.length).toBeGreaterThanOrEqual(6);
  });
});

describe("truncation", () => {
  it("says a chart is cut short only when it is", () => {
    expect(truncationNote(false, 10)).toBeNull();
    expect(truncationNote(true, 5000)).toBe("Showing the first 5,000 rows: the result was cut short.");
  });
});
