const UNITS = ["B", "KB", "MB", "GB", "TB"];

/** A file's size for people: "512 B", "1.5 KB", "20 MB" (binary units, as file managers do). */
export function formatBytes(bytes: number): string {
  let unit = 0;
  let value = bytes;
  // Up a unit at 1024, or at what would round to 1024 (1023.95 KB is "1.0 MB", not "1024 KB").
  while (unit < UNITS.length - 1 && value >= (unit === 0 ? 1024 : 1023.95)) {
    value /= 1024;
    unit++;
  }
  if (unit === 0) return `${value} B`;
  // One decimal place for what shows as under 10 (9.96 shows as "10", not "10.0").
  return `${value < 9.95 ? value.toFixed(1) : Math.round(value)} ${UNITS[unit]}`;
}
