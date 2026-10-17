import { Notice } from "@/components/Notice";
import type { DataStoreStatus } from "@/lib/api";
import { dataStoreNotice } from "@/lib/data-store";

/**
 * Says why a data store that isn't ready holds things up, or, while its status is still
 * unknown, that it couldn't be read (`error`, while it keeps trying); nothing once it's ready.
 * `action` is what the user can do once it is, e.g. "upload documents".
 */
export function DataStoreNotice({
  status,
  error,
  action,
  className,
}: {
  status: DataStoreStatus | undefined;
  error?: string;
  action: string;
  className?: string;
}) {
  // Once the status is known, a later failure to read it again leaves the last one standing.
  if (!status) {
    return error ? (
      <Notice label="Data store" busy className={className}>
        {error}
      </Notice>
    ) : null;
  }
  const notice = dataStoreNotice(status, action);
  if (!notice) return null;
  // A store that couldn't be set up is an error, shown as other errors are; one being set up is
  // progress.
  return (
    <Notice
      label="Data store"
      error={status === "failed"}
      busy={status === "provisioning"}
      className={className}
    >
      {notice}
    </Notice>
  );
}
