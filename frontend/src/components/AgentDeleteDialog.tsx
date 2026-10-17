import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { ExternalLink, RefreshCw } from "lucide-react";

import { ErrorBanner } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { RunStatusBadge } from "@/components/workflow/status";
import {
  hasDependents,
  parseDeleteRefusal,
  resolutions,
  unresolved,
} from "@/lib/agent-delete";
import {
  api,
  describeError,
  type AgentDependents,
  type DependentRequest,
  type DependentTrigger,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;

/** The runs listed, as one string to compare: the same runs, or not. */
const runKeys = (d?: AgentDependents) =>
  d ? [...d.agent_runs.map((r) => `a${r.id}`), ...d.workflow_runs.map((r) => `w${r.id}`)].join() : "";

/** A link out of the dialog: it opens in a new tab, so the dialog keeps what's been chosen (and
 * checks again when the user comes back). */
function Out({ to, className, children }: { to: string; className?: string; children: ReactNode }) {
  return (
    <Link
      to={to}
      target="_blank"
      rel="noopener"
      className={cn("inline-flex min-w-0 items-center gap-1 hover:underline", className)}
    >
      <span className="truncate">{children}</span>
      <ExternalLink className="text-muted-foreground size-3 shrink-0" aria-hidden />
    </Link>
  );
}

function Requests({ requests }: { requests: DependentRequest[] }) {
  if (!requests.length) return null;
  return (
    <ul className="mt-1 flex flex-col gap-0.5 pl-3 text-xs">
      {requests.map((r) => (
        <li key={r.id}>
          <Out to={`/data?tab=changes&request=${encodeURIComponent(r.id)}`}>
            Schema change: {r.summary}
          </Out>
        </li>
      ))}
    </ul>
  );
}

function Schedules({ triggers, to }: { triggers: DependentTrigger[]; to: string }) {
  if (!triggers.length) return null;
  return (
    <ul className="mt-1 flex flex-col gap-0.5 text-xs">
      {triggers.map((t) => (
        <li key={t.id}>
          <Out to={to}>
            Schedule: {t.description} ({t.timezone}){t.enabled ? "" : " · paused"}
          </Out>
        </li>
      ))}
    </ul>
  );
}

/**
 * Deleting an agent: first checks what depends on it (`GET /api/agents/{id}/dependents`). With
 * nothing, a simple confirm. Otherwise it lists the runs still going (with the requests they
 * wait on), the workflows that run the agent and the schedules that would start them, each a
 * link that opens in a new tab, and Delete stays disabled until each has a resolution: cancel
 * the runs, and for each workflow, deactivate it or edit it to stop running the agent (which
 * takes it off the list once it's done; the list is checked again when the window regains
 * focus, or on "Check again").
 */
export function AgentDeleteDialog({
  agent,
  onClose,
  onDeleted,
}: {
  agent: { id: number; name: string };
  onClose: () => void;
  onDeleted: () => void | Promise<void>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [dependents, setDependents] = useState<AgentDependents>();
  const [loadError, setLoadError] = useState<string>();
  const [refusal, setRefusal] = useState<string>();
  const [cancelRuns, setCancelRuns] = useState(false);
  // By workflow id: what the user chose for it.
  const [choice, setChoice] = useState<Map<number, "deactivate" | "edit">>(new Map());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    ref.current?.showModal();
  }, []);

  /** Take a fresh list: choices for what's still listed carry over, but the runs must be agreed
   * to again if they're not the same runs. */
  const shown = useRef<AgentDependents>(undefined);
  const take = useCallback((next: AgentDependents) => {
    if (runKeys(shown.current) !== runKeys(next)) setCancelRuns(false);
    shown.current = next;
    setDependents(next);
    setChoice((prev) => new Map([...prev].filter(([id]) => next.workflows.some((w) => w.id === id))));
  }, []);

  const load = useCallback(async () => {
    setLoadError(undefined);
    try {
      take(await api.getAgentDependents(agent.id));
    } catch (err) {
      setLoadError(describeError(err));
    }
  }, [agent.id, take]);

  useEffect(() => {
    load();
    // Back from a link (a workflow edited in another tab, say): check again.
    window.addEventListener("focus", load);
    return () => window.removeEventListener("focus", load);
  }, [load]);

  const deactivate = new Set([...choice].filter(([, c]) => c === "deactivate").map(([id]) => id));
  const choices = { cancelRuns, deactivate };
  const blocked = !dependents || unresolved(dependents, choices).length > 0;

  async function onDelete() {
    if (!dependents) return;
    setBusy(true);
    setRefusal(undefined);
    try {
      await api.deleteAgent(
        agent.id,
        hasDependents(dependents) ? resolutions(dependents, choices) : undefined,
      );
      await onDeleted();
    } catch (err) {
      const refused = parseDeleteRefusal(err);
      if (refused) take(refused.dependents);
      setRefusal(refused?.message ?? describeError(err));
    } finally {
      setBusy(false);
    }
  }

  const runs = dependents ? dependents.agent_runs.length + dependents.workflow_runs.length : 0;
  const own = dependents?.triggers ?? [];

  return (
    <dialog
      ref={ref}
      aria-labelledby="agent-delete-title"
      // Escape, or the Cancel button: either closes it.
      onClose={onClose}
      className="bg-background text-foreground m-auto w-[min(36rem,calc(100vw-2rem))] rounded-lg border p-0 shadow-lg backdrop:bg-black/40"
    >
      <div className="flex max-h-[80vh] flex-col">
        {/* Only the body scrolls: the actions stay in view however long the list. */}
        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-5 pb-3">
          <h2 id="agent-delete-title" className="text-lg font-semibold">
            Delete “{agent.name}”?
          </h2>
          <ErrorBanner message={loadError} />
          <ErrorBanner message={refusal} onDismiss={() => setRefusal(undefined)} />
          {!dependents && !loadError && (
            <p className="text-muted-foreground text-sm">Checking what uses this agent…</p>
          )}
          {dependents && !hasDependents(dependents) && (
            <p className="text-sm">
              Delete the agent “{agent.name}”, its versions and all of its runs?
            </p>
          )}
          {dependents && hasDependents(dependents) && (
            <p className="text-sm">
              Some things still use “{agent.name}”. Choose what happens to each before deleting
              it, with its versions and all of its runs. Links open in a new tab.
            </p>
          )}

          {dependents && runs > 0 && (
            <section className="flex flex-col gap-2">
              <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
                Runs still going
              </h3>
              <ul aria-label="Runs still going" className="divide-y rounded-md border text-sm">
                {dependents.agent_runs.map((r) => (
                  <li key={`a${r.id}`} className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <Out to={`/agents/${agent.id}/runs/${r.id}`} className="flex-1">
                        {r.title ?? "Untitled run"}
                      </Out>
                      <span className="text-muted-foreground shrink-0 text-xs">
                        {r.waiting ? "Waiting for a decision" : "Running a turn"}
                      </span>
                    </div>
                    <Requests requests={r.requests} />
                  </li>
                ))}
                {dependents.workflow_runs.map((r) => (
                  <li key={`w${r.id}`} className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <Out to={`/workflows/${r.workflow_id}/runs/${r.id}`} className="flex-1">
                        {r.workflow_name} · run {r.id}
                      </Out>
                      <RunStatusBadge status={r.status} />
                    </div>
                    {r.waiting_nodes.length > 0 && (
                      <p className="text-muted-foreground mt-1 pl-3 text-xs">
                        Waiting at {r.waiting_nodes.join(", ")}
                      </p>
                    )}
                    <Requests requests={r.requests} />
                  </li>
                ))}
              </ul>
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={cancelRuns}
                  onChange={(e) => setCancelRuns(e.target.checked)}
                />
                <span>
                  Cancel these runs
                  <span className="text-muted-foreground block text-xs">
                    Workflow runs fail and chat runs are deleted with the agent; a turn underway
                    stops, their pending schema change requests are cancelled, and nothing
                    resumes.
                  </span>
                </span>
              </label>
            </section>
          )}

          {dependents && dependents.workflows.length > 0 && (
            <section className="flex flex-col gap-2">
              <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
                Workflows that run it
              </h3>
              {dependents.workflows.map((w) => {
                const chosen = choice.get(w.id);
                const scheduled = w.triggers.filter((t) => t.enabled).length;
                const set = (c: "deactivate" | "edit") =>
                  setChoice((prev) => new Map(prev).set(w.id, c));
                return (
                  <div
                    key={w.id}
                    role="group"
                    aria-label={w.name}
                    className="rounded-md border px-3 py-2 text-sm"
                  >
                    <div className="flex items-center gap-2">
                      <Out to={`/workflows/${w.id}/edit`} className="flex-1 font-medium">
                        {w.name}
                      </Out>
                      <span className="text-muted-foreground shrink-0 text-xs">
                        {w.direct ? "runs it" : "runs it through a nested workflow"} ·{" "}
                        {plural(scheduled, "schedule")}
                      </span>
                    </div>
                    <Schedules triggers={w.triggers} to={`/workflows/${w.id}`} />
                    <div className="mt-2 flex flex-wrap gap-2">
                      <Button
                        size="sm"
                        variant={chosen === "deactivate" ? "default" : "outline"}
                        aria-pressed={chosen === "deactivate"}
                        onClick={() => set("deactivate")}
                      >
                        Deactivate
                      </Button>
                      <Button
                        size="sm"
                        variant={chosen === "edit" ? "default" : "outline"}
                        aria-pressed={chosen === "edit"}
                        onClick={() => set("edit")}
                      >
                        Edit workflow
                      </Button>
                    </div>
                    {chosen === "deactivate" && (
                      <p className="text-muted-foreground mt-1 text-xs">
                        {scheduled === 0
                          ? "No run of it can start"
                          : `${scheduled === 1 ? "Its schedule is" : `Its ${scheduled} schedules are`} turned off, and no run of it can start`}{" "}
                        until it's edited to stop running this agent.
                      </p>
                    )}
                    {chosen === "edit" && (
                      <p className="text-muted-foreground mt-1 text-xs">
                        Edit it to stop running this agent
                        {w.direct ? "" : " (take the nested workflow's newer version, or remove it)"}
                        ; it leaves this list once it's saved.{" "}
                        <Out to={`/workflows/${w.id}/edit`} className="text-foreground underline">
                          Open the editor
                        </Out>
                      </p>
                    )}
                  </div>
                );
              })}
            </section>
          )}

          {dependents && own.length > 0 && (
            <section className="flex flex-col gap-1">
              <p className="text-muted-foreground text-xs">
                Its {plural(own.length, "schedule")} {own.length === 1 ? "is" : "are"} deleted
                with it.
              </p>
              <Schedules triggers={own} to={`/agents/${agent.id}?tab=schedule`} />
            </section>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t px-5 py-3">
          <Button variant="destructive" disabled={blocked || busy} onClick={onDelete}>
            Delete agent
          </Button>
          <Button variant="ghost" onClick={() => ref.current?.close()}>
            Cancel
          </Button>
          {dependents && hasDependents(dependents) && (
            <Button variant="ghost" className="ml-auto" onClick={load} disabled={busy}>
              <RefreshCw />
              Check again
            </Button>
          )}
        </div>
      </div>
    </dialog>
  );
}
