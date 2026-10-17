import { ApiError, type AgentDeleteResolutions, type AgentDependents } from "@/lib/api";

/** What the user has chosen in the delete dialog. */
export type DeleteChoices = {
  /** Cancel the runs still going (all those listed). */
  cancelRuns: boolean;
  /** The workflows to deactivate, by id. The others must be edited first. */
  deactivate: Set<number>;
};

/** Whether deleting the agent would cut anything off. Its own schedules just go with it. */
export function hasDependents(d: AgentDependents): boolean {
  return d.agent_runs.length > 0 || d.workflow_runs.length > 0 || d.workflows.length > 0;
}

/** What still needs a choice before the agent can be deleted: "runs", then "workflow:<id>"
 * for each workflow neither deactivated nor (yet) edited to stop running it. */
export function unresolved(d: AgentDependents, choices: DeleteChoices): string[] {
  const left: string[] = [];
  if ((d.agent_runs.length || d.workflow_runs.length) && !choices.cancelRuns) left.push("runs");
  for (const w of d.workflows) if (!choices.deactivate.has(w.id)) left.push(`workflow:${w.id}`);
  return left;
}

/** The delete's body: the listed runs, named one by one (so one that starts meanwhile isn't
 * cancelled unseen: the server refuses instead), and the listed workflows to deactivate. */
export function resolutions(d: AgentDependents, choices: DeleteChoices): AgentDeleteResolutions {
  return {
    cancel_agent_runs: choices.cancelRuns ? d.agent_runs.map((r) => r.id) : [],
    cancel_workflow_runs: choices.cancelRuns ? d.workflow_runs.map((r) => r.id) : [],
    deactivate_workflows: d.workflows.filter((w) => choices.deactivate.has(w.id)).map((w) => w.id),
  };
}

/** A delete refused because something still depends on the agent: why, and what does now. */
export function parseDeleteRefusal(
  err: unknown,
): { message: string; dependents: AgentDependents } | undefined {
  if (!(err instanceof ApiError) || err.status !== 409) return undefined;
  try {
    const detail = JSON.parse(err.message)?.detail;
    if (typeof detail?.message === "string" && detail.dependents) {
      return { message: detail.message, dependents: detail.dependents };
    }
  } catch {
    // not JSON
  }
  return undefined;
}
