/** Whether this browser can show times in `timezone`: the server knows some its Intl doesn't. */
export function browserKnowsTimeZone(timezone: string) {
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: timezone });
    return true;
  } catch {
    return false;
  }
}

/** `iso` as a date and time in `timezone`, or in UTC if this browser doesn't know it. */
export function formatInTimeZone(iso: string, timezone: string) {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: browserKnowsTimeZone(timezone) ? timezone : "UTC",
  });
}
