import { useCallback, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";
import { ArrowLeft, KeyRound, Loader2, Pause, Play, RefreshCw } from "lucide-react";

import { DetailPage, ErrorBanner, Field, formatDate, RoleNote, Section, useLoad, usePoll } from "@/components/crud";
import { ShownOnce, SourceBadge } from "@/components/Sources";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, describeError, type SourceDetail, type SourceSync } from "@/lib/api";
import { useRefusal } from "@/lib/current-user";
import { canChangeSource, intervalLabel, isPushed, parseSenders, sourceKindLabel, syncSummary } from "@/lib/sources";

const SYNC_KIND: Record<SourceSync["kind"], string> = {
  incremental: "Scheduled",
  full: "Full",
  manual: "Sync now",
};

// How often the page reads the source again while a sync of it runs (ms).
const RUNNING_POLL_MS = 3000;

/**
 * One source's page (`/data/sources/:sourceId`): its health, its recent syncs (what each added,
 * updated, removed and skipped, and its error), and, for admins, Sync now, Full resync, Pause and
 * Resume. Editors and participants see those disabled, with why; Uploads has none. A webhook or
 * email source is sent to rather than synced: it shows its URL (an email source's address is shown
 * only once), what arrived, and a new secret or address for admins; an email source its allowed
 * senders.
 */
export function SourcePage() {
  const sourceId = Number(useParams().sourceId);
  const { data: source, error: loadError, reload } = useLoad(
    useCallback(() => api.getSource(sourceId), [sourceId]),
  );
  const refusal = useRefusal("sources");
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();
  const [rotated, setRotated] = useState<SourceDetail>();
  const running = source?.syncs.some((s) => s.status === "running") ?? false;
  usePoll(running || busy === "sync" || busy === "full", reload, RUNNING_POLL_MS);

  async function act(what: string, call: () => Promise<unknown>) {
    setBusy(what);
    setError(undefined);
    try {
      await call();
      await reload();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(undefined);
    }
  }

  if (!source)
    return (
      <DetailPage title="Source">
        <ErrorBanner message={loadError} />
        {!loadError && <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />}
      </DetailPage>
    );

  const changeable = canChangeSource(source);
  const pushed = isPushed(source.kind);
  const paused = source.status === "paused";
  const disabled = !!refusal || busy !== undefined || source.status === "removing";
  return (
    <div className="h-full overflow-y-auto">
      <DetailPage
        title={
          <span className="flex items-center gap-2">
            {source.name} <SourceBadge source={source} />
          </span>
        }
        subtitle={<Subtitle source={source} />}
        actions={
          changeable && (
            <>
              {pushed ? (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={disabled}
                  title={refusal ?? "The old one stops working at once"}
                  onClick={() => act("rotate", async () => setRotated(await api.rotateSourceSecret(source.id)))}
                >
                  {busy === "rotate" ? <Loader2 className="animate-spin" /> : <KeyRound />}
                  {source.kind === "email" ? "New address" : "New secret"}
                </Button>
              ) : (
                <>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={disabled || paused || running}
                    title={refusal ?? (running ? "It's syncing now." : undefined)}
                    onClick={() => act("sync", () => api.syncSource(source.id))}
                  >
                    {busy === "sync" ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                    Sync now
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={disabled || paused || running}
                    title={refusal ?? "Read everything in scope again, removing what's gone"}
                    onClick={() => act("full", () => api.syncSource(source.id, true))}
                  >
                    Full resync
                  </Button>
                </>
              )}
              <Button
                size="sm"
                variant="outline"
                disabled={disabled}
                title={refusal ?? undefined}
                onClick={() => act("pause", () => api.updateSource(source.id, { paused: !paused }))}
              >
                {paused ? <Play /> : <Pause />}
                {paused ? "Resume" : "Pause"}
              </Button>
            </>
          )
        }
      >
        <Link to="/data/sources" className="text-muted-foreground flex items-center gap-1 text-sm hover:underline">
          <ArrowLeft className="size-4" /> Sources
        </Link>
        {changeable && <RoleNote refusal={refusal} />}
        <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
        {rotated && <ShownOnce source={rotated} onDone={() => setRotated(undefined)} />}
        <Settings source={source} disabled={disabled} refusal={refusal} onSaved={reload} />
        {changeable ? (
          <Syncs source={source} />
        ) : (
          <p className="text-muted-foreground text-sm">
            Uploads doesn't sync: it holds what members upload, and is always on.
          </p>
        )}
      </DetailPage>
    </div>
  );
}

/** What a URL, webhook or email source points at: its page, its endpoint, or its allowed senders
 * (which admins change here). */
function Settings({
  source,
  disabled,
  refusal,
  onSaved,
}: {
  source: SourceDetail;
  disabled: boolean;
  refusal: string | null | undefined;
  onSaved: () => Promise<unknown>;
}) {
  const [senders, setSenders] = useState((source.allowed_senders ?? []).join("\n"));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(undefined);
    try {
      await api.updateSource(source.id, { allowed_senders: parseSenders(senders) });
      await onSaved();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  if (source.kind === "url" && source.url)
    return (
      <p className="text-sm">
        Fetches{" "}
        <a href={source.url} target="_blank" rel="noreferrer noopener" className="break-all underline">
          {source.url}
        </a>
      </p>
    );
  if (source.kind === "webhook" && source.webhook_url)
    return (
      <Field
        label="Webhook URL"
        htmlFor="source-webhook-url"
        hint="Sign each call with its secret, shown when it was made."
      >
        <Input id="source-webhook-url" readOnly value={source.webhook_url} onFocus={(e) => e.target.select()} />
      </Field>
    );
  if (source.kind !== "email") return null;
  const none = (source.allowed_senders ?? []).length === 0;
  return (
    <form onSubmit={save} className="flex flex-col gap-2" aria-label="Allowed senders">
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      <Field
        label="Allowed senders"
        htmlFor="source-senders"
        hint={
          none
            ? "No one is allowed yet, so nothing is kept. One per line: an address, or a domain as @example.com."
            : "One per line: an address, or a domain as @example.com. Mail from anyone else is dropped."
        }
      >
        <Textarea
          id="source-senders"
          value={senders}
          onChange={(e) => setSenders(e.target.value)}
          rows={3}
          disabled={disabled}
          title={refusal ?? undefined}
        />
      </Field>
      <div>
        <Button type="submit" size="sm" variant="outline" disabled={disabled || saving}>
          {saving && <Loader2 className="animate-spin" />}
          Save senders
        </Button>
      </div>
    </form>
  );
}

function Subtitle({ source }: { source: SourceDetail }) {
  const every = intervalLabel(source.interval_seconds);
  return (
    <>
      {sourceKindLabel(source.kind)}
      {source.provider && ` (${source.provider})`} · {source.document_count}{" "}
      {source.document_count === 1 ? "document" : "documents"}
      {source.kind !== "upload" && ` · ${source.item_count} ${source.item_count === 1 ? "item" : "items"}`}
      {every && ` · syncs ${every}`}
      {source.next_sync_at && ` · next ${formatDate(source.next_sync_at)}`}
    </>
  );
}

function Syncs({ source }: { source: SourceDetail }) {
  return (
    <Section title="Recent syncs" label="Recent syncs">
      {source.syncs.length === 0 && (
        <p className="text-muted-foreground text-sm">
          {isPushed(source.kind) ? "Nothing has arrived yet." : "It hasn't synced yet."}
        </p>
      )}
      {source.syncs.length > 0 && (
        <ul className="divide-y rounded-md border" aria-label="Syncs">
          {source.syncs.map((sync) => (
            <li key={sync.id} className="flex items-start gap-3 px-3 py-2 text-sm">
              <div className="min-w-0 flex-1">
                <p>
                  {isPushed(source.kind) ? "Received" : SYNC_KIND[sync.kind]} · {formatDate(sync.started_at)}
                </p>
                <p className="text-muted-foreground text-xs">{syncSummary(sync)}</p>
                {sync.error && (
                  <p
                    className={
                      sync.status === "failed"
                        ? "text-destructive mt-0.5 text-xs break-words"
                        : "text-muted-foreground mt-0.5 text-xs break-words"
                    }
                  >
                    {sync.error}
                  </p>
                )}
              </div>
              <Badge variant={sync.status === "failed" ? "destructive" : sync.status === "running" ? "secondary" : "outline"}>
                {sync.status === "running" && <Loader2 className="animate-spin" />}
                {sync.status === "succeeded" ? "Succeeded" : sync.status === "failed" ? "Failed" : "Running"}
              </Badge>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
