/**
 * Prompted and saved charts on the Insights page: drawing an agent's Vega-Lite spec with the
 * rows of its query, in the app's light and dark themes. Specs come checked by the API against
 * an allowlist (no data of their own, links, images, parameters or expressions); the page draws
 * them with the pinned Vega-Lite runtime and its CSP-safe expression interpreter.
 */

export type ChartRow = Record<string, unknown>;
export type ChartSpec = Record<string, unknown>;
export type ColorScheme = "light" | "dark";

/** The spec as drawn: the query's rows inline (never data of the spec's own), filling the
 * width it's given unless it sets one. */
export function drawnSpec(spec: ChartSpec, rows: ChartRow[]): ChartSpec {
  const { data: _data, $schema: _schema, ...rest } = spec;
  return {
    width: "container",
    autosize: { type: "fit", contains: "padding" },
    ...rest,
    data: { values: rows },
  };
}

// The app's tokens (`index.css`) as plain colours, which SVG attributes take.
const TOKENS: Record<ColorScheme, { text: string; muted: string; grid: string; domain: string }> = {
  light: { text: "#0a0a0a", muted: "#737373", grid: "#e5e5e5", domain: "#a3a3a3" },
  dark: { text: "#fafafa", muted: "#a3a3a3", grid: "#2e2e2e", domain: "#525252" },
};

// Categories, readable on both backgrounds.
const CATEGORY: Record<ColorScheme, string[]> = {
  light: ["#2563eb", "#16a34a", "#d97706", "#dc2626", "#7c3aed", "#0891b2", "#db2777", "#65a30d"],
  dark: ["#60a5fa", "#4ade80", "#fbbf24", "#f87171", "#a78bfa", "#22d3ee", "#f472b6", "#a3e635"],
};

/** Vega-Lite config drawing a chart in the app's colours. */
export function chartTheme(scheme: ColorScheme) {
  const t = TOKENS[scheme];
  const category = CATEGORY[scheme];
  return {
    background: "transparent",
    font: "ui-sans-serif, system-ui, sans-serif",
    title: { color: t.text, subtitleColor: t.muted, fontWeight: 600 },
    axis: {
      labelColor: t.muted,
      titleColor: t.text,
      gridColor: t.grid,
      domainColor: t.domain,
      tickColor: t.domain,
    },
    legend: { labelColor: t.muted, titleColor: t.text },
    header: { labelColor: t.muted, titleColor: t.text },
    view: { stroke: "transparent" },
    mark: { color: category[0] },
    text: { color: t.text },
    range: { category },
  };
}

/** What a truncated chart says under it; null if it isn't. */
export function truncationNote(truncated: boolean, rows: number): string | null {
  if (!truncated) return null;
  return `Showing the first ${rows.toLocaleString("en-GB")} rows: the result was cut short.`;
}
