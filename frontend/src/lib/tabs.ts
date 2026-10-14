/** The tab to show: the one asked for (e.g. in the URL) if it exists, else the first. */
export function activeTab(ids: string[], requested: string | null): string | undefined {
  return requested !== null && ids.includes(requested) ? requested : ids[0];
}

/**
 * The tab a key press in the tab list moves to, as the ARIA tabs pattern has it: the arrows
 * step (wrapping at the ends), Home and End jump. Null for any other key.
 */
export function keyTarget(key: string, index: number, count: number): number | null {
  switch (key) {
    case "ArrowRight":
      return (index + 1) % count;
    case "ArrowLeft":
      return (index - 1 + count) % count;
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}
