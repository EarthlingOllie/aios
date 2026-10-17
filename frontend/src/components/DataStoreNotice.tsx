import { Loader2 } from "lucide-react";

import type { DataStoreStatus } from "@/lib/api";
import { dataStoreNotice } from "@/lib/data-store";
import { cn } from "@/lib/utils";

/**
 * Says why a data store that isn't ready holds things up; nothing while its status is unknown or
 * once it's ready. `action` is what the user can do once it is, e.g. "upload documents".
 */
export function DataStoreNotice({
  status,
  action,
  className,
}: {
  status: DataStoreStatus | undefined;
  action: string;
  className?: string;
}) {
  const notice = status && dataStoreNotice(status, action);
  if (!notice) return null;
  // A store that couldn't be set up is an error, shown as other errors are; one being set up is
  // progress.
  return (
    <div
      role={status === "failed" ? "alert" : "status"}
      aria-label="Data store"
      className={cn(
        "flex items-start gap-2 rounded-md border text-sm",
        status === "failed" ? "border-destructive/50 text-destructive" : "text-muted-foreground",
        className,
      )}
    >
      {status === "provisioning" && <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin" />}
      <span>{notice}</span>
    </div>
  );
}
