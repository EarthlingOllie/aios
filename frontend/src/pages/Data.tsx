import { useEffect } from "react";
import { Loader2 } from "lucide-react";

import { DocumentsSection, useDataStore } from "@/components/Documents";
import { TablesSection } from "@/components/Tables";
import { Tabs } from "@/components/tabs";
import { Badge } from "@/components/ui/badge";
import type { DataStoreStatus, DataStoreUsage } from "@/lib/api";
import { dataStoreSummary, storageMeters, type StorageMeter } from "@/lib/data-store";
import { cn } from "@/lib/utils";

// How often the Overview asks about the store again while it's open (ms): its usage is measured
// every few minutes.
const USAGE_REFRESH_MS = 60_000;

const statusVariant = {
  provisioning: "secondary",
  ready: "outline",
  failed: "destructive",
  deleting: "secondary",
  deleted: "secondary",
} as const;

/**
 * What's in the user's data store. Each section is a tab with its own URL (`/data` for the
 * Overview, `/data?tab=tables`, `/data?tab=documents`), so other pages and notifications can
 * link straight to one.
 */
export function DataPage() {
  // Held above the routes, so switching tabs (or pages) doesn't ask again or drop an upload.
  const { status: storeStatus, usage, error: storeError, recheck } = useDataStore();
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6 md:px-8">
        <header>
          <h1 className="text-xl font-semibold">Data</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            What your agents keep in your data store, and the documents they search.
          </p>
        </header>
        <Tabs
          label="Data sections"
          tabs={[
            {
              id: "overview",
              label: "Overview",
              content: (
                <Overview status={storeStatus} usage={usage} error={storeError} recheck={recheck} />
              ),
            },
            { id: "tables", label: "Tables", content: <TablesSection /> },
            { id: "documents", label: "Documents", content: <DocumentsSection /> },
          ]}
        />
      </div>
    </div>
  );
}

function Overview({
  status,
  usage,
  error,
  recheck,
}: {
  status: DataStoreStatus | undefined;
  usage: DataStoreUsage | undefined;
  error?: string;
  recheck: () => void;
}) {
  const summary = status && dataStoreSummary(status);
  // Usage changes as the schedule measures it, so it's asked about again while it's in view.
  useEffect(() => {
    const timer = setInterval(recheck, USAGE_REFRESH_MS);
    return () => clearInterval(timer);
  }, [recheck]);
  return (
    <section aria-label="Data store" className="flex flex-col gap-2 rounded-md border px-4 py-3">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">Data store</h2>
        {status && summary ? (
          <Badge variant={statusVariant[status]}>
            {status === "provisioning" && <Loader2 className="animate-spin" />}
            {summary.label}
          </Badge>
        ) : (
          <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />
        )}
      </div>
      {summary && <p className="text-muted-foreground text-sm">{summary.message}</p>}
      {error && <p className="text-destructive text-sm">{error}</p>}
      {status === "ready" && usage && <Usage usage={usage} />}
    </section>
  );
}

const barColour = {
  unknown: "bg-muted-foreground",
  ok: "bg-primary",
  near: "bg-amber-500",
  full: "bg-destructive",
} as const;

/** The store's usage against each of its caps, with what happens near or at one. */
function Usage({ usage }: { usage: DataStoreUsage }) {
  return (
    <div className="mt-2 flex flex-col gap-3">
      <h3 className="text-sm font-semibold">Storage</h3>
      {storageMeters(usage).map((m) => (
        <UsageMeter key={m.id} meter={m} />
      ))}
      {usage.measured_at && (
        <p className="text-muted-foreground text-xs">
          Measured {new Date(usage.measured_at).toLocaleString()}
        </p>
      )}
    </div>
  );
}

function UsageMeter({ meter }: { meter: StorageMeter }) {
  const percent = Math.round(meter.share * 100);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span id={`usage-${meter.id}`}>{meter.label}</span>
        <span className="text-muted-foreground">{meter.used}</span>
      </div>
      <div
        role="meter"
        aria-labelledby={`usage-${meter.id}`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-valuetext={meter.used}
        className="bg-muted h-2 overflow-hidden rounded-full"
      >
        <div
          className={cn("h-full rounded-full", barColour[meter.level])}
          style={{ width: `${percent}%` }}
        />
      </div>
      {meter.warning && (
        <p
          role={meter.level === "full" ? "alert" : undefined}
          className={cn(
            "text-sm",
            meter.level === "full" ? "text-destructive" : "text-amber-700 dark:text-amber-400",
          )}
        >
          {meter.warning}
        </p>
      )}
    </div>
  );
}
