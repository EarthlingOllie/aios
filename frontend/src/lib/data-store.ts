import { useEffect, useState } from "react";

import { api, type DataStoreStatus } from "@/lib/api";

/**
 * What to say about a data store that isn't ready, with what the user can do once it is (e.g.
 * "upload documents"); null once it's ready.
 */
export function dataStoreNotice(status: DataStoreStatus, action: string): string | null {
  switch (status) {
    case "ready":
      return null;
    case "provisioning":
      return `Setting up your data store. You can ${action} once it's ready.`;
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

/**
 * The user's data store's status, asked again while it isn't ready. Undefined until the first
 * answer; a failed request keeps the last one and tries again.
 */
export function useDataStoreStatus(): DataStoreStatus | undefined {
  const [status, setStatus] = useState<DataStoreStatus>();
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = () =>
      api
        .getDataStore()
        .then(({ status }) => {
          if (stopped) return;
          setStatus(status);
          const wait = dataStorePollInterval(status);
          if (wait !== null) timer = setTimeout(check, wait);
        })
        .catch(() => {
          if (!stopped) timer = setTimeout(check, 5000);
        });
    check();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, []);
  return status;
}
