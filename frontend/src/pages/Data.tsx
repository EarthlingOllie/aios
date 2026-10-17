import { Loader2 } from "lucide-react";

import { DocumentsSection, useSharedDocuments } from "@/components/Documents";
import { Tabs } from "@/components/tabs";
import { Badge } from "@/components/ui/badge";
import type { DataStoreStatus } from "@/lib/api";
import { dataStoreSummary } from "@/lib/data-store";

const statusVariant = {
  provisioning: "secondary",
  ready: "outline",
  failed: "destructive",
  deleting: "secondary",
  deleted: "secondary",
} as const;

/**
 * What's in the user's data store. Each section is a tab with its own URL (`/data` for the
 * Overview, `/data?tab=documents`), so other pages and notifications can link straight to one.
 */
export function DataPage() {
  // Held above the routes, so switching tabs (or pages) doesn't ask again or drop an upload.
  const { storeStatus, storeError } = useSharedDocuments();
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
              content: <Overview status={storeStatus} error={storeError} />,
            },
            { id: "documents", label: "Documents", content: <DocumentsSection /> },
          ]}
        />
      </div>
    </div>
  );
}

function Overview({ status, error }: { status: DataStoreStatus | undefined; error?: string }) {
  const summary = status && dataStoreSummary(status);
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
    </section>
  );
}
