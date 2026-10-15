import type { DataStoreStatus } from "@/lib/api";

/** What the documents panel says about a data store that isn't ready; null once it is. */
export function dataStoreNotice(status: DataStoreStatus): string | null {
  switch (status) {
    case "ready":
      return null;
    case "provisioning":
      return "Setting up your data store. You can upload documents once it's ready.";
    case "failed":
      return "Your data store couldn't be set up. We'll try again; please check back later.";
    default:
      return "Your data store isn't available.";
  }
}

/** How long to wait before asking again about a store that isn't ready (ms); null once it is. */
export function dataStorePollInterval(status: DataStoreStatus): number | null {
  if (status === "ready") return null;
  // Setting up takes seconds; a failed store waits for a retry, which takes much longer.
  return status === "provisioning" ? 2000 : 15000;
}
