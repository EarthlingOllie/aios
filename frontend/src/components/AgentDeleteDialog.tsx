import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { RefreshCw } from "lucide-react";

import { ErrorBanner } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { RunStatusBadge } from "@/components/workflow/status";
import {
  hasDependents,
  parseDeleteRefusal,
  resolutions,
  unresolved,
} from "@/lib/agent-delete";
import { api, describeError, type AgentDependents } from "@/lib/api";

const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;

/** The runs listed, as one string to compare: the same runs, or not. */
const runKeys = (d?: AgentDependents) =>
  d ? [...d.agent_runs.map((r) => `a${r.id}`), ...d.workflow_runs.map((r) => `w${r.id}`)].join() : "";

/**
 * Deleting an agent: first checks what depends on it (`GET /api/agents/{id}/dependents`). With
 * nothing, a simple confirm. Otherwise it lists the runs still going and the workflows that run
 * the agent, and Delete stays disabled until each has a resolution: cancel the runs, and for
 * each workflow, deactivate it or edit it to stop running the agent (which takes it off the
 * list once it's done).
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
  const own = dependents?.triggers.length ?? 0;

  return (
    <dialog
      ref={ref}
      aria-labelledby="agent-delete-title"
      // Escape, or the Cancel button: either closes it.
      onClose={onClose}
      className="bg-background text-foreground m-auto w-[min(36rem,calc(100vw-2rem))] rounded-lg border p-0 shadow-lg backdrop:bg-black/40"
    >
      <div className="flex max-h-[80vh] flex-col gap-4 overflow-y-auto p-5">
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
            Some things still use “{agent.name}”. Choose what happens to each before deleting it,
            with its versions and all of its runs.
          </p>
        )}

        {dependents && runs > 0 && (
          <section className="flex flex-col gap-2">
            <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
              Runs still going
            </h3>
            <ul className="divide-y rounded-md border text-sm">
              {dependents.agent_runs.map((r) => (
                <li key={`a${r.id}`} className="flex items-center gap-2 px-3 py-2">
                  <Link
                    to={`/agents/${agent.id}/runs/${r.id}`}
                    className="min-w-0 flex-1 truncate hover:underline"
                  >
                    {r.title ?? "Untitled run"}
                  </Link>
                  <span className="text-muted-foreground shrink-0 text-xs">
                    {r.waiting ? "Waiting for a decision" : "Running a turn"}
                  </span>
                </li>
              ))}
              {dependents.workflow_runs.map((r) => (
                <li key={`w${r.id}`} className="flex items-center gap-2 px-3 py-2">
                  <Link
                    to={`/workflows/${r.workflow_id}/runs/${r.id}`}
                    className="min-w-0 flex-1 truncate hover:underline"
                  >
                    {r.workflow_name} · run {r.id}
                  </Link>
                  <RunStatusBadge status={r.status} />
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
                  Workflow runs fail and chat runs are deleted with the agent; their pending
                  schema change requests are cancelled, and nothing resumes.
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
              const set = (c: "deactivate" | "edit") =>
                setChoice((prev) => new Map(prev).set(w.id, c));
              return (
                <fieldset key={w.id} className="rounded-md border px-3 py-2 text-sm">
                  <legend className="sr-only">{w.name}</legend>
                  <div className="flex items-center gap-2">
                    <Link to={`/workflows/${w.id}`} className="min-w-0 flex-1 truncate font-medium hover:underline">
                      {w.name}
                    </Link>
                    <span className="text-muted-foreground shrink-0 text-xs">
                      {w.direct ? "runs it" : "runs it through a nested workflow"} ·{" "}
                      {plural(w.triggers.filter((t) => t.enabled).length, "schedule")}
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
                    <label className="flex items-center gap-1.5">
                      <input
                        type="radio"
                        name={`workflow-${w.id}`}
                        checked={chosen === "deactivate"}
                        onChange={() => set("deactivate")}
                      />
                      Deactivate
                    </label>
                    <label className="flex items-center gap-1.5">
                      <input
                        type="radio"
                        name={`workflow-${w.id}`}
                        checked={chosen === "edit"}
                        onChange={() => set("edit")}
                      />
                      Edit workflow
                    </label>
                  </div>
                  {chosen === "deactivate" && (
                    <p className="text-muted-foreground mt-1 text-xs">
                      Its schedules are turned off, and no run of it can start until it's edited
                      to stop running this agent.
                    </p>
                  )}
                  {chosen === "edit" && (
                    <p className="text-muted-foreground mt-1 text-xs">
                      Edit it to stop running this agent
                      {w.direct ? "" : " (take the nested workflow's newer version, or remove it)"},
                      then check again.{" "}
                      <Link to={`/workflows/${w.id}/edit`} className="text-foreground underline">
                        Open the editor
                      </Link>
                    </p>
                  )}
                </fieldset>
              );
            })}
          </section>
        )}

        {dependents && own > 0 && (
          <p className="text-muted-foreground text-xs">
            Its {plural(own, "schedule")} {own === 1 ? "is" : "are"} deleted with it.
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2">
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
