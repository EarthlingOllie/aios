import { useState } from "react";
import { Loader2 } from "lucide-react";

import { ErrorBanner, formatDate, Section, useLoad } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { api, describeError, type FlaggedDocument, type FlaggedSyncedItem } from "@/lib/api";
import { personName } from "@/lib/provenance";

/**
 * The Organisation page's flagged list, admins' only: each document and synced item Jev flagged as
 * a likely prompt injection, with its reason. Releasing one makes it searchable again (agents still treat
 * it as untrusted); the release is recorded in the audit log.
 */
export function FlaggedSection() {
  const { data: flagged, error: loadError, reload } = useLoad(api.listFlagged);
  const items = useLoad(api.listFlaggedItems);
  const [busy, setBusy] = useState<number>();
  const [error, setError] = useState<string>();

  async function releaseItem(item: FlaggedSyncedItem) {
    if (
      !window.confirm(
        `Release ${item.title}, from ${item.source_name}? Agents and search will use it again, as untrusted content, until it changes at its source. This is recorded in the audit log.`,
      )
    )
      return;
    setBusy(-item.id);
    setError(undefined);
    try {
      await api.releaseFlaggedItem(item.id);
      await items.reload();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(undefined);
    }
  }

  async function release(doc: FlaggedDocument) {
    if (
      !window.confirm(
        `Release ${doc.filename}? Agents and search will use it again, as untrusted content. This is recorded in the audit log.`,
      )
    )
      return;
    setBusy(doc.id);
    setError(undefined);
    try {
      await api.releaseFlagged(doc.id);
      await reload();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(undefined);
    }
  }

  return (
    <Section title="Flagged content" label="Flagged content">
      <p className="text-muted-foreground text-sm">
        Documents and synced items Jev, the prompt-injection check, flagged. They're kept, but agents and search
        can't use them. Release one you trust.
      </p>
      <ErrorBanner message={error ?? loadError ?? items.error} onDismiss={() => setError(undefined)} />
      {!flagged && !loadError && <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />}
      {flagged && flagged.length === 0 && items.data?.length === 0 && (
        <p className="text-muted-foreground text-sm">Nothing is flagged.</p>
      )}
      {flagged && flagged.length > 0 && (
        <ul className="divide-y rounded-md border" aria-label="Flagged documents">
          {flagged.map((doc) => (
            <li key={doc.id} className="flex items-start gap-3 px-3 py-2 text-sm">
              <div className="min-w-0 flex-1">
                <p className="truncate">{doc.filename}</p>
                <p className="text-destructive mt-0.5 text-xs break-words">{doc.reason ?? "Flagged by Jev."}</p>
                <p className="text-muted-foreground text-xs">
                  Uploaded {formatDate(doc.created_at)}
                  {doc.uploaded_by && ` by ${personName(doc.uploaded_by)}`}
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                disabled={busy !== undefined}
                onClick={() => release(doc)}
                aria-label={`Release ${doc.filename}`}
              >
                {busy === doc.id && <Loader2 className="animate-spin" />}
                Release
              </Button>
            </li>
          ))}
        </ul>
      )}
      {items.data && items.data.length > 0 && (
        <ul className="divide-y rounded-md border" aria-label="Flagged synced items">
          {items.data.map((item) => (
            <li key={item.id} className="flex items-start gap-3 px-3 py-2 text-sm">
              <div className="min-w-0 flex-1">
                <p className="truncate">
                  {item.url ? (
                    <a href={item.url} target="_blank" rel="noreferrer" className="hover:underline">
                      {item.title}
                    </a>
                  ) : (
                    item.title
                  )}
                </p>
                <p className="text-destructive mt-0.5 text-xs break-words">{item.reason ?? "Flagged by Jev."}</p>
                <p className="text-muted-foreground text-xs">
                  Synced from {item.source_name} {formatDate(item.created_at)}
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                disabled={busy !== undefined}
                onClick={() => releaseItem(item)}
                aria-label={`Release ${item.title}`}
              >
                {busy === -item.id && <Loader2 className="animate-spin" />}
                Release
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
