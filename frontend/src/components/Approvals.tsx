import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { Check, Database, Loader2, ShieldCheck, TriangleAlert, X } from "lucide-react";

import { ErrorBanner, formatDate, selectClass, useLoad, usePoll } from "@/components/crud";
import { Notice } from "@/components/Notice";
import { useRefreshNotifications } from "@/components/Notifications";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  describeError,
  type ApprovalKind,
  type JsonValue,
  type SchemaChangeRequest,
  type SchemaChangeStatus,
} from "@/lib/api";
import { confirmed, removalText } from "@/lib/dropped";
import { approvalNeeds, isStep, requestSummary } from "@/lib/schema-changes";
import { cn } from "@/lib/utils";

/** How often a request the platform is still working on is read again (ms). */
const POLL_MS = 2000;

/**
 * An approval, with approve and reject (with an optional note) while it's pending and the user
 * may decide it; anyone else sees who may, and no buttons. A schema
 * change shows the agent's reason and the exact SQL that would run; a workflow's approval step,
 * its full prompt and its input, and the run waiting on it. Shown at the agent's tool call in
 * its chat, on a waiting workflow run's page, and in the Data page's Approvals list.
 */
export function ApprovalCard({
  request,
  onDecided,
  showRun = false,
  foldSql = false,
  current = false,
}: {
  request: SchemaChangeRequest;
  /** After a decision, with the request as the server has it now. */
  onDecided: (request: SchemaChangeRequest) => void;
  /** Link to the run that asked (the Data page; a chat is that run). */
  showRun?: boolean;
  /** Keep the SQL folded away until asked for (the Data page's history). */
  foldSql?: boolean;
  /** The one a link (a notification) opened: marked, and scrolled into view. */
  current?: boolean;
}) {
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string>();
  // For a request that drops anything: the tables' names, as the owner typed them.
  const [typed, setTyped] = useState("");
  const summary = requestSummary(request);
  // Who may decide it while it's pending (the approval rules), for everyone to see.
  const needs = approvalNeeds(request);
  const confirmWith = request.can_decide ? request.confirm_with : null;
  const step = isStep(request);
  const refreshNotifications = useRefreshNotifications();

  async function decide(decision: () => Promise<SchemaChangeRequest>) {
    setSending(true);
    setError(undefined);
    try {
      onDecided(await decision());
      // Deciding it clears its notification: the badge shouldn't wait for the next poll.
      void refreshNotifications();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSending(false);
    }
  }

  const section = useRef<HTMLElement>(null);
  useEffect(() => {
    if (current) section.current?.scrollIntoView?.({ block: "nearest" });
  }, [current]);

  return (
    <section
      ref={section}
      aria-label={`${step ? "Approval" : "Schema change"}: ${request.summary}`}
      aria-current={current || undefined}
      className={cn(
        "flex flex-col gap-3 rounded-md border px-4 py-3 text-sm",
        current && "ring-primary ring-2",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        {step ? (
          <ShieldCheck className="text-muted-foreground size-4" />
        ) : (
          <Database className="text-muted-foreground size-4" />
        )}
        <h3 className="font-medium">{request.summary}</h3>
        <Badge variant={summary.variant} className="ml-auto">
          {summary.inFlight && <Loader2 className="animate-spin" />}
          {summary.label}
        </Badge>
      </div>
      {step ? (
        <StepDetails request={request} />
      ) : (
        <>
          <p>
            <span className="text-muted-foreground">The agent's reason: </span>
            {request.reason}
          </p>
          {foldSql ? (
            <details>
              <summary className="text-muted-foreground cursor-pointer text-xs">Show the SQL</summary>
              <SqlBlock sql={request.sql ?? ""} />
            </details>
          ) : (
            <SqlBlock sql={request.sql ?? ""} />
          )}
        </>
      )}
      <div className="text-muted-foreground flex flex-wrap gap-x-3 text-xs">
        <span>Asked {formatDate(request.created_at)}</span>
        {request.decided_at && (
          <span>
            {summary.decidedLabel} {formatDate(request.decided_at)}
            {request.decided_by && ` by ${request.decided_by.name}`}
          </span>
        )}
        {request.status === "pending" && request.expires_at && (
          <span>Expires {formatDate(request.expires_at)}</span>
        )}
        {showRun && request.step && (
          <Link
            to={`/workflows/${request.step.workflow_id}/runs/${request.step.run_id}`}
            className="hover:text-foreground underline"
          >
            From {request.step.workflow_name}, run #{request.step.run_id}
          </Link>
        )}
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
      {needs && (
        <p role="note" aria-label="Who may approve" className="text-muted-foreground text-xs">
          {needs}
        </p>
      )}
      {confirmWith && <Removals request={request} />}
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      {confirmWith && !rejecting && (
        <label className="flex flex-col gap-1 text-xs">
          <span>
            It can be restored from the Dropped section until it's purged. Type{" "}
            <code className="bg-muted rounded px-1 font-mono">{confirmWith}</code> to confirm.
          </span>
          <Input
            aria-label={`Type ${confirmWith} to confirm`}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            className="max-w-xs font-mono"
          />
        </label>
      )}
      {request.can_decide &&
        (rejecting ? (
          <form
            className="flex flex-col gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              decide(() => api.rejectApproval(request.id, note.trim()));
            }}
          >
            <Textarea
              aria-label={step ? "Note (optional)" : "Note for the agent (optional)"}
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
              disabled={sending || (!!confirmWith && !confirmed(typed, confirmWith))}
              onClick={() =>
                decide(() => api.approveApproval(request.id, confirmWith ? typed : undefined))
              }
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

/** What a pending request's drops would remove from the store as it is now. */
function Removals({ request }: { request: SchemaChangeRequest }) {
  const impact = useLoad(useCallback(() => api.approvalImpact(request.id), [request.id]));
  return (
    <div className="border-destructive/40 flex flex-col gap-1 rounded-md border px-3 py-2">
      <p className="text-destructive flex items-center gap-1.5 font-medium">
        <TriangleAlert className="size-4" /> This drops data
      </p>
      {impact.error && <p className="text-destructive text-xs">{impact.error}</p>}
      {!impact.data && !impact.error && (
        <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />
      )}
      {impact.data && (
        <ul aria-label="What it removes" className="list-disc pl-5 text-xs">
          {impact.data.removes.map((r, i) => (
            <li key={i}>{removalText(r)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SqlBlock({ sql }: { sql: string }) {
  return (
    <pre
      aria-label="SQL"
      className="bg-muted/50 mt-1 max-h-72 overflow-auto rounded p-2 font-mono text-xs whitespace-pre-wrap"
    >
      {sql}
    </pre>
  );
}

/** A workflow approval step's prompt, in full, and its input: what the approver decides on. */
function StepDetails({ request }: { request: SchemaChangeRequest }) {
  const input = request.input;
  return (
    <>
      <p aria-label="Prompt" className="whitespace-pre-wrap">
        {request.reason}
      </p>
      {input !== null && input !== undefined && (
        <div className="flex flex-col gap-1">
          <span className="text-muted-foreground text-xs">Its input</span>
          <pre
            aria-label="Input"
            className="bg-muted/50 max-h-72 overflow-auto rounded p-2 font-mono text-xs whitespace-pre-wrap"
          >
            {jsonText(input)}
          </pre>
        </div>
      )}
      {request.step && (
        <p className="text-muted-foreground text-xs">
          Asked by step <span className="font-mono">{request.step.path.join(" / ")}</span>
        </p>
      )}
    </>
  );
}

const jsonText = (value: JsonValue) =>
  typeof value === "string" ? value : JSON.stringify(value, null, 2);

const KINDS: { value: ApprovalKind | ""; label: string }[] = [
  { value: "", label: "Every kind" },
  { value: "schema_change", label: "Schema changes" },
  { value: "workflow_step", label: "Workflow approvals" },
];

const STATUSES: { value: SchemaChangeStatus | ""; label: string }[] = [
  { value: "", label: "Every status" },
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
  { value: "expired", label: "Expired" },
  { value: "cancelled", label: "Cancelled" },
  { value: "applied", label: "Applied" },
  { value: "failed", label: "Failed" },
  { value: "stale", label: "No longer applies" },
];

/**
 * The Data page's Approvals list: schema changes and workflow approval steps, filterable by kind
 * and status (kept in the URL, `?kind=&status=`), those waiting first, then the history of those
 * decided. Read again while any is still being applied or its run is carrying on.
 */
export function ApprovalsSection() {
  // The approval a link (a notification) opened, if any: `/data/approvals?approval=<id>` (or an
  // old link's `?request=<id>`).
  const [params, setParams] = useSearchParams();
  const opened = params.get("approval") ?? params.get("request");
  const kind = (params.get("kind") ?? "") as ApprovalKind | "";
  const status = (params.get("status") ?? "") as SchemaChangeStatus | "";
  const list = useLoad(
    useCallback(
      () => api.listApprovals({ kind: kind || undefined, status: status || undefined }),
      [kind, status],
    ),
  );
  const requests = list.data ?? [];
  usePoll(
    requests.some((r) => requestSummary(r).inFlight),
    list.reload,
    POLL_MS,
  );
  const pending = requests.filter((r) => r.status === "pending");
  const decided = requests.filter((r) => r.status !== "pending");
  const filter = (name: "kind" | "status", value: string) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (value) next.set(name, value);
        else next.delete(name);
        return next;
      },
      { replace: true },
    );

  return (
    <div className="flex flex-col gap-6">
      <div role="group" aria-label="Filters" className="flex flex-wrap gap-3">
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">Kind</span>
          <select className={selectClass} value={kind} onChange={(e) => filter("kind", e.target.value)}>
            {KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">Status</span>
          <select
            className={selectClass}
            value={status}
            onChange={(e) => filter("status", e.target.value)}
          >
            {STATUSES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {list.error && (
        <Notice label="Approvals" error className="px-3 py-2">
          {list.error}
        </Notice>
      )}
      {(!status || status === "pending") && (
        <section aria-label="Pending approvals" className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold">Waiting for a decision</h2>
          {!list.data && !list.error && (
            <Loader2 className="text-muted-foreground size-4 animate-spin" aria-label="Loading" />
          )}
          {list.data && pending.length === 0 && (
            <p className="text-muted-foreground text-sm">
              Nothing is waiting. When an agent asks to change your tables, or a workflow reaches an
              approval step, it shows here.
            </p>
          )}
          {pending.map((r) => (
            <ApprovalCard
              key={r.id}
              request={r}
              onDecided={() => list.reload()}
              showRun
              current={r.id === opened}
            />
          ))}
        </section>
      )}
      {decided.length > 0 && (
        <section aria-label="Approval history" className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold">History</h2>
          {decided.map((r) => (
            <ApprovalCard
              key={r.id}
              request={r}
              onDecided={() => list.reload()}
              showRun
              foldSql
              current={r.id === opened}
            />
          ))}
        </section>
      )}
      {list.data && status && status !== "pending" && decided.length === 0 && (
        <p className="text-muted-foreground text-sm">No approvals match.</p>
      )}
    </div>
  );
}

/** The old name: a schema change's card is an approval's. */
export const SchemaChangeCard = ApprovalCard;
