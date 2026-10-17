/**
 * A waiting workflow run's page: the nodes waiting for the user to decide a schema change
 * request, with each request's card (approve and reject), and the pins it will resume on that
 * have changed since it started.
 */
import { useCallback } from "react";
import { Link } from "react-router";
import { Database, MessageSquare } from "lucide-react";

import { useLoad } from "@/components/crud";
import { Notice } from "@/components/Notice";
import { SchemaChangeCard } from "@/components/SchemaChanges";
import { Button } from "@/components/ui/button";
import { api, type SchemaChangeRequest, type StalePin, type WorkflowRunStep } from "@/lib/api";

/** The requests the run's waiting steps wait on, by id; read again with `reload`. */
export function useWaitingRequests(steps: WorkflowRunStep[]) {
  const key = steps.flatMap((s) => s.waiting_on_request_ids).join(",");
  const load = useLoad(
    useCallback(async () => {
      const ids = key ? key.split(",") : [];
      const requests = await Promise.all(ids.map((id) => api.getSchemaChange(id)));
      return Object.fromEntries(requests.map((r) => [r.id, r]));
    }, [key]),
  );
  return { requests: load.data ?? {}, error: load.error, reload: load.reload };
}

/** Each waiting agent node, with the request it waits on. */
export function WaitingSection({
  steps,
  requests,
  error,
  onDecided,
}: {
  steps: WorkflowRunStep[];
  requests: Record<string, SchemaChangeRequest>;
  error?: string;
  onDecided: () => void;
}) {
  const waiting = steps.filter((s) => s.waiting_on_request_ids.length > 0);
  if (waiting.length === 0) return null;
  const count = waiting.flatMap((s) => s.waiting_on_request_ids).length;
  return (
    <section
      aria-label="Waiting for your decision"
      className="flex flex-col gap-3 rounded-md border border-amber-500/50 px-4 py-3"
    >
      <h2 className="text-sm font-semibold">Waiting for your decision</h2>
      <p className="text-muted-foreground text-sm">
        The run is paused, holding no worker, until you approve or reject{" "}
        {count === 1 ? "this request" : "these requests"}. It then carries on from
        where it stopped.
      </p>
      {error && (
        <Notice label="Requests" error className="px-3 py-2">
          Couldn't load the request: {error}
        </Notice>
      )}
      {waiting.map((step) => {
        const asks = step.waiting_on_request_ids.map((id) => requests[id]).filter(Boolean);
        return (
          <div key={step.id} className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-mono">{step.path.join(" / ")}</span>
              <span className="text-muted-foreground">asks:</span>
              <span className="ml-auto flex gap-2">
                {step.agent_id !== null && step.agent_run_id !== null && (
                  <Button asChild size="sm" variant="ghost">
                    <Link to={`/agents/${step.agent_id}/runs/${step.agent_run_id}`}>
                      <MessageSquare />
                      Open the conversation
                    </Link>
                  </Button>
                )}
                <Button asChild size="sm" variant="ghost">
                  <Link to="/data?tab=changes">
                    <Database />
                    All schema changes
                  </Link>
                </Button>
              </span>
            </div>
            {asks.map((request) => (
              <SchemaChangeCard key={request.id} request={request} onDecided={onDecided} />
            ))}
          </div>
        );
      })}
    </section>
  );
}

function describePin(pin: StalePin) {
  const where = pin.path.length === 0 ? "This workflow" : pin.path.join(" / ");
  const now =
    pin.current_version !== null
      ? `now v${pin.current_version}`
      : pin.kind === "agent"
        ? "deleted: this node fails if it runs"
        : "deleted";
  return `${where}: ${pin.name} v${pin.pinned_version} (${now})`;
}

/** The pins a waiting run resumes on that are no longer current; nothing if there are none. */
export function StalePins({ pins }: { pins: StalePin[] }) {
  if (pins.length === 0) return null;
  return (
    <section
      aria-label="Pinned versions"
      className="text-muted-foreground flex flex-col gap-1 rounded-md border px-4 py-3 text-sm"
    >
      <p>It resumes on the versions it started with, though these have changed since:</p>
      <ul className="list-disc pl-5">
        {pins.map((pin) => (
          <li key={pin.path.join("/")}>{describePin(pin)}</li>
        ))}
      </ul>
    </section>
  );
}
