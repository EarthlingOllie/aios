import { useCallback, useState } from "react";
import { Link } from "react-router";
import { Check, Database, Loader2, X } from "lucide-react";

import { ErrorBanner, formatDate, useLoad, usePoll } from "@/components/crud";
import { Notice } from "@/components/Notice";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { api, describeError, type SchemaChangeRequest } from "@/lib/api";
import { requestSummary } from "@/lib/schema-changes";
import { cn } from "@/lib/utils";

/** How often a request the platform is still working on is read again (ms). */
const POLL_MS = 2000;

/**
 * A schema change request: the agent's reason and the exact SQL that would run, with approve
 * and reject (with an optional note) while it's pending and the user may decide it. Shown at
 * the agent's tool call in its chat, and in the Data page's Changes section.
 */
export function SchemaChangeCard({
  request,
  onDecided,
  showRun = false,
}: {
  request: SchemaChangeRequest;
  /** After a decision, with the request as the server has it now. */
  onDecided: (request: SchemaChangeRequest) => void;
  /** Link to the run that asked (the Data page; a chat is that run). */
  showRun?: boolean;
}) {
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string>();
  const summary = requestSummary(request);

  async function decide(decision: () => Promise<SchemaChangeRequest>) {
    setSending(true);
    setError(undefined);
    try {
      onDecided(await decision());
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSending(false);
    }
  }

  return (
    <section
      aria-label={`Schema change: ${request.summary}`}
      className="flex flex-col gap-3 rounded-md border px-4 py-3 text-sm"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Database className="text-muted-foreground size-4" />
        <h3 className="font-medium">{request.summary}</h3>
        <Badge variant={summary.variant} className="ml-auto">
          {summary.inFlight && <Loader2 className="animate-spin" />}
          {summary.label}
        </Badge>
      </div>
      <p>
        <span className="text-muted-foreground">The agent's reason: </span>
        {request.reason}
      </p>
      <pre
        aria-label="SQL"
        className="bg-muted/50 max-h-72 overflow-auto rounded p-2 font-mono text-xs whitespace-pre-wrap"
      >
        {request.sql}
      </pre>
      <div className="text-muted-foreground flex flex-wrap gap-x-3 text-xs">
        <span>Asked {formatDate(request.created_at)}</span>
        {request.decided_at && <span>Decided {formatDate(request.decided_at)}</span>}
        {showRun && request.run && (
          <Link
            to={`/agents/${request.run.agent_id}/runs/${request.run.run_id}`}
            className="hover:text-foreground underline"
          >
            {request.run.title ? `From run “${request.run.title}”` : `From run #${request.run.run_id}`}
          </Link>
        )}
      </div>
      {request.status !== "pending" && (
        <p className={cn(summary.variant === "destructive" ? "text-destructive" : "text-muted-foreground")}>
          {summary.message}
        </p>
      )}
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {request.can_decide &&
        (rejecting ? (
          <form
            className="flex flex-col gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              decide(() => api.rejectSchemaChange(request.id, note.trim()));
            }}
          >
            <Textarea
              aria-label="Note for the agent (optional)"
              placeholder="Why not, or what to do instead (optional)"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={2000}
            />
            <div className="flex gap-2">
              <Button type="submit" variant="destructive" size="sm" disabled={sending}>
                Reject
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => setRejecting(false)}>
                Back
              </Button>
            </div>
          </form>
        ) : (
          <div className="flex gap-2">
            <Button
              size="sm"
              disabled={sending}
              onClick={() => decide(() => api.approveSchemaChange(request.id))}
            >
              <Check /> Approve
            </Button>
            <Button size="sm" variant="outline" disabled={sending} onClick={() => setRejecting(true)}>
              <X /> Reject…
            </Button>
          </div>
        ))}
    </section>
  );
}

/**
 * The Data page's Changes section: the schema change requests waiting for the user first, then
 * the history of those decided. Read again while any is still being applied or its run is
 * carrying on.
 */
export function ChangesSection() {
  const list = useLoad(useCallback(() => api.listSchemaChanges(), []));
  const requests = list.data ?? [];
  usePoll(
    requests.some((r) => requestSummary(r).inFlight),
    list.reload,
    POLL_MS,
  );
  const pending = requests.filter((r) => r.status === "pending");
  const decided = requests.filter((r) => r.status !== "pending");

  return (
    <div className="flex flex-col gap-6">
      {list.error && (
        <Notice label="Changes" error className="px-3 py-2">
          {list.error}
        </Notice>
      )}
      <section aria-label="Pending changes" className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold">Waiting for you</h2>
        {!list.data && !list.error && (
          <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />
        )}
        {list.data && pending.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No changes are waiting. When an agent asks to change your tables, it shows here.
          </p>
        )}
        {pending.map((r) => (
          <SchemaChangeCard key={r.id} request={r} onDecided={() => list.reload()} showRun />
        ))}
      </section>
      {decided.length > 0 && (
        <section aria-label="Change history" className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold">History</h2>
          {decided.map((r) => (
            <SchemaChangeCard key={r.id} request={r} onDecided={() => list.reload()} showRun />
          ))}
        </section>
      )}
    </div>
  );
}
