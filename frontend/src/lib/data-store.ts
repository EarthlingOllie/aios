import { useCallback, useEffect, useState } from "react";

import { api, describeError, type DataStoreStatus, type DataStoreUsage } from "@/lib/api";
import { formatBytes } from "@/lib/format-bytes";

// The notice and the Data page's Overview say the same thing about a store that failed or is
// otherwise unavailable (the failed wording also matches the API's 409). They differ only while
// it's being set up.
const FAILED_MESSAGE =
  "The organisation's data store couldn't be set up. We'll try again; please check back later.";
const UNAVAILABLE_MESSAGE = "The organisation's data store isn't available.";

/**
 * What to say about a data store that isn't ready, with what the user can do once it is (e.g.
 * "upload documents"); null once it's ready.
 */
export function dataStoreNotice(status: DataStoreStatus, action: string): string | null {
  switch (status) {
    case "ready":
      return null;
    case "provisioning":
      return `Setting up the organisation's data store. You can ${action} once it's ready.`;
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
          "The organisation's data store is ready. Its agents can keep data in it, and you can upload documents.",
      };
    case "provisioning":
      return {
        label: "Being set up",
        message:
          "We're setting up the organisation's data store. This usually takes a few seconds; you can upload documents once it's ready.",
      };
    case "failed":
      return { label: "Failed", message: FAILED_MESSAGE };
    default:
      return { label: "Unavailable", message: UNAVAILABLE_MESSAGE };
  }
}

// From this share of a cap, the Overview warns of what happens at it.
const NEAR_CAP = 0.8;

export type StorageMeter = {
  id: "db" | "files";
  label: string;
  /** E.g. "12 MB of 1.0 GB". */
  used: string;
  /** Of the cap, from 0 to 1 (past the cap, 1). */
  share: number;
  level: "unknown" | "ok" | "near" | "full";
  /** What happens near or at the cap; null otherwise. */
  warning: string | null;
};

// At either cap, the API refuses uploads and agents' inserts and updates alike.
const WARNINGS = {
  db: {
    near: "Your data is nearly at its cap. Once it is, your agents can't add or change rows, and documents can't be uploaded.",
    full: "Your data is at its cap: your agents can't add or change rows, and documents can't be uploaded. Reading still works, and deleting rows makes room.",
  },
  files: {
    near: "Your documents are nearly at their cap. Once they are, uploads are refused, and your agents can't add or change rows.",
    full: "Your documents are at their cap: uploads are refused, and your agents can't add or change rows. Delete some to make room.",
  },
};

function meter(
  id: StorageMeter["id"],
  label: string,
  bytes: number | null,
  quota: number,
): StorageMeter {
  if (bytes === null) {
    return { id, label, used: "Not measured yet", share: 0, level: "unknown", warning: null };
  }
  const share = Math.min(bytes / quota, 1);
  // As the API refuses writes: at the cap, not only past it.
  const level = bytes >= quota ? "full" : share >= NEAR_CAP ? "near" : "ok";
  return {
    id,
    label,
    used: `${formatBytes(bytes)} of ${formatBytes(quota)}`,
    share,
    level,
    warning: level === "ok" ? null : WARNINGS[id][level],
  };
}

/** The store's usage against each of its caps, for the Data page's Overview. */
export function storageMeters(usage: DataStoreUsage): StorageMeter[] {
  return [
    // The data its tables hold (not its database's size, which deleting rows doesn't bring down).
    meter("db", "Data stored", usage.db_bytes, usage.db_quota_bytes),
    meter("files", "Documents", usage.files_bytes, usage.files_quota_bytes),
  ];
}

/** Whether either cap was last seen reached, so uploads are refused. */
export function uploadsRefused(usage: DataStoreUsage | undefined): boolean {
  return Boolean(usage && storageMeters(usage).some((m) => m.level === "full"));
}

/** What uploads are refused for, at either cap; empty below both. */
export function fullWarnings(usage: DataStoreUsage | undefined): string[] {
  if (!usage) return [];
  return storageMeters(usage).flatMap((m) => (m.level === "full" && m.warning ? [m.warning] : []));
}

/** How long to wait before asking again about a store that isn't ready (ms); null once it is. */
export function dataStorePollInterval(status: DataStoreStatus): number | null {
  if (status === "ready") return null;
  // Setting up takes seconds; a failed store waits for a retry, which takes much longer.
  return status === "provisioning" ? 2000 : 15000;
}

/**
 * The organisation's data store's status and usage, asked again while it isn't ready. `status` and
 * `usage` are undefined until the first answer; a failed request keeps the last ones, says what
 * went wrong in `error`, and tries again. `recheck` asks again straight away, e.g. about a store
 * last seen ready, which nothing polls.
 */
export function useDataStoreStatus(): {
  status?: DataStoreStatus;
  usage?: DataStoreUsage;
  error?: string;
  recheck: () => void;
} {
  const [status, setStatus] = useState<DataStoreStatus>();
  const [usage, setUsage] = useState<DataStoreUsage>();
  const [error, setError] = useState<string>();
  // Bumped by `recheck`: starts the checks over, dropping any still waiting or in flight.
  const [round, setRound] = useState(0);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = () =>
      api
        .getDataStore()
        .then(({ status, usage }) => {
          if (stopped) return;
          setStatus(status);
          setUsage(usage);
          setError(undefined);
          const wait = dataStorePollInterval(status);
          if (wait !== null) timer = setTimeout(check, wait);
        })
        .catch((err) => {
          if (stopped) return;
          setError(`Couldn't check the organisation's data store (${describeError(err)}). Trying again…`);
          timer = setTimeout(check, 5000);
        });
    check();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [round]);
  const recheck = useCallback(() => setRound((n) => n + 1), []);
  return { status, usage, error, recheck };
}
