import { useCallback } from "react";
import { Inbox, Loader2, Upload } from "lucide-react";

import { useLoad } from "@/components/crud";
import { Notice } from "@/components/Notice";
import { Badge } from "@/components/ui/badge";
import { api, type Source } from "@/lib/api";
import { sourceKindLabel, sourceStatusLabel } from "@/lib/sources";

const statusVariant = {
  active: "outline",
  paused: "secondary",
  failing: "destructive",
  needs_reauth: "destructive",
  removing: "secondary",
} as const;

/**
 * The Data page's Sources: what brings content into the organisation's store, for every member to
 * see. Today that's Uploads, which every uploaded document belongs to; it's always on, so it
 * offers nothing to pause or remove.
 */
export function SourcesSection() {
  const list = useLoad(useCallback(() => api.listSources(), []));
  return (
    <section className="flex flex-col gap-3">
      <p className="text-muted-foreground text-sm">
        Where the organisation's documents come from. Everything members upload belongs to
        Uploads.
      </p>
      {list.error && (
        <Notice label="Sources" error className="px-3 py-2">
          {list.error}
        </Notice>
      )}
      {!list.data && !list.error && (
        <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />
      )}
      {list.data && list.data.length > 0 && (
        <ul className="divide-y rounded-md border" aria-label="Sources">
          {list.data.map((source) => (
            <SourceRow key={source.id} source={source} />
          ))}
        </ul>
      )}
    </section>
  );
}

function SourceRow({ source }: { source: Source }) {
  const Icon = source.kind === "upload" ? Upload : Inbox;
  const count = source.document_count;
  return (
    <li className="flex items-center gap-3 px-3 py-2">
      <Icon className="text-muted-foreground size-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm">{source.name}</p>
        <p className="text-muted-foreground text-xs">
          {sourceKindLabel(source.kind)} · {count} {count === 1 ? "document" : "documents"}
        </p>
      </div>
      <Badge variant={statusVariant[source.status]}>{sourceStatusLabel(source.status)}</Badge>
    </li>
  );
}
