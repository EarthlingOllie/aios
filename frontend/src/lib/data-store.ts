import type { DataStoreStatus } from "@/lib/api";

// The notice and the Overview say the same thing about a store that failed or is otherwise
// unavailable (the failed wording also matches the API's 409). They differ only while it's set up.
const FAILED_MESSAGE =
  "Your data store couldn't be set up. We'll try again; please check back later.";
const UNAVAILABLE_MESSAGE = "Your data store isn't available.";

/** What the documents panel says about a data store that isn't ready; null once it is. */
export function dataStoreNotice(status: DataStoreStatus): string | null {
  switch (status) {
    case "ready":
      return null;
    case "provisioning":
      return "Setting up your data store. You can upload documents once it's ready.";
    case "failed":
      return FAILED_MESSAGE;
    default:
      return UNAVAILABLE_MESSAGE;
  }
}

/** How the Data page's Overview names a store's status, and what it says about it. */
export function dataStoreSummary(status: DataStoreStatus): { label: string; message: string } {
  switch (status) {
    case "ready":
      return {
        label: "Ready",
        message:
          "Your data store is ready. Your agents can keep data in it, and you can upload documents.",
      };
    case "provisioning":
      return {
        label: "Being set up",
        message:
          "We're setting up your data store. This usually takes a few seconds; you can upload documents once it's ready.",
      };
    case "failed":
      return { label: "Failed", message: FAILED_MESSAGE };
    default:
      return { label: "Unavailable", message: UNAVAILABLE_MESSAGE };
  }
}

/** How long to wait before asking again about a store that isn't ready (ms); null once it is. */
export function dataStorePollInterval(status: DataStoreStatus): number | null {
  if (status === "ready") return null;
  // Setting up takes seconds; a failed store waits for a retry, which takes much longer.
  return status === "provisioning" ? 2000 : 15000;
}
