import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Link,
  useBlocker,
  useNavigate,
  useOutletContext,
  useParams,
  useSearchParams,
} from "react-router";
import { Archive, ArchiveRestore, MessagesSquare, Pencil, Play } from "lucide-react";

import {
  DetailPage,
  ErrorBanner,
  Field,
  formatDate,
  MasterDetail,
  Placeholder,
  RoleNote,
  Section,
  useListHref,
  useLoad,
  usePoll,
  type ListContext,
} from "@/components/crud";
import { WorkflowArchiveDialog } from "@/components/ArchiveDialog";
import { ExportLink, ImportButton, UnrunnableNotice } from "@/components/ImportDialog";
import { GuardFields, guardsValid } from "@/components/Limits";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ProvenanceLine, StartedBy, useSavedBy } from "@/components/provenance";
import { TargetSchedules } from "@/components/schedules";
import { Tabs } from "@/components/tabs";
import { Textarea } from "@/components/ui/textarea";
import { RestoreButton, VersionBadge, VersionHistory } from "@/components/versions";
import { WorkflowCanvas, type Selection } from "@/components/workflow/canvas";
import { GraphEditor } from "@/components/workflow/editor";
import { Inspector } from "@/components/workflow/inspector";
import { JsonField, SchemaInputForm } from "@/components/workflow/structured";
import { RunStatusBadge } from "@/components/workflow/status";
import {
  api,
  describeError,
  type NodePosition,
  type WorkflowSummary,
  type WorkflowVersion,
  type InvocableBy,
  type JsonValue,
  type Limits,
} from "@/lib/api";
import { plural } from "@/lib/utils";
import { fromVersion, setSchemas, toInput, type Draft, type Target } from "@/lib/editor";
import {
  nameAndDescription,
  NAME_SORT,
  UPDATED_SORT,
  type ListFilterGroup,
  type ListSort,
} from "@/lib/list-filter";
import { useRefusal, useRole } from "@/lib/current-user";
import { draftOf, limitsOf, RUN_LIMITS, type LimitsDraft } from "@/lib/limits";
import { personName } from "@/lib/provenance";
import { isActive, WAITING_POLL_MS } from "@/lib/runs";
import { ArchivedBadge } from "@/pages/Agents";

const WORKFLOW_FILTERS: ListFilterGroup<WorkflowSummary>[] = [
  {
    param: "filter",
    label: "Show",
    all: "All",
    options: [
      { key: "empty", label: "Empty", test: (workflow) => workflow.node_count === 0 },
      // The list is loaded from the archived workflows for this one (`WorkflowsSection`); the
      // others narrow the default list, which leaves archived workflows out.
      { key: "archived", label: "Archived", test: (workflow) => workflow.archived_at !== null },
    ],
  },
];

const WORKFLOW_SORTS: ListSort<WorkflowSummary>[] = [NAME_SORT, UPDATED_SORT];

export function WorkflowsSection() {
  const [params] = useSearchParams();
  const archived = params.get("filter") === "archived";
  const { data, error, reload } = useLoad(
    useCallback(() => api.listWorkflows(archived), [archived]),
  );
  const newRefusal = useRefusal("build");
  return (
    <MasterDetail
      title="Workflows"
      newRefusal={newRefusal}
      actions={<ImportButton onImported={reload} />}
      basePath="/workflows"
      items={data}
      error={error}
      empty="No workflows yet. A workflow chains agents, and other workflows, into a graph."
      reloadList={reload}
      search={nameAndDescription}
      filters={WORKFLOW_FILTERS}
      sorts={WORKFLOW_SORTS}
      renderItem={(workflow) => (
        <>
          <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate font-medium">{workflow.name}</span>
            {workflow.archived_at && <ArchivedBadge />}
            <VersionBadge version={workflow.version} />
          </div>
          <div className="text-muted-foreground truncate text-xs">
            {plural(workflow.node_count, "node")}
            {workflow.description && ` · ${workflow.description}`}
          </div>
          <ProvenanceLine of={workflow} />
        </>
      )}
    />
  );
}

export function WorkflowsIndex() {
  return <Placeholder>Select a workflow, or create a new one.</Placeholder>;
}

/** A version's graph, with the inspector for whatever is selected on it. */
export function GraphView({
  version,
  layout,
  editHref,
}: {
  version: WorkflowVersion;
  layout: Record<string, NodePosition>;
  /** Where to build the graph, if it's empty and can be edited. */
  editHref?: string;
}) {
  const [selection, setSelection] = useState<Selection | null>(null);
  if (version.nodes.length === 0) {
    return (
      <p className="text-muted-foreground rounded-md border p-6 text-center text-sm">
        This workflow has no nodes yet.
        {editHref && (
          <>
            {" "}
            <Link to={editHref} className="underline">
              Add some
            </Link>
            .
          </>
        )}
      </p>
    );
  }
  return (
    // The inspector goes beside the canvas only when that leaves the canvas room to be legible.
    <div className="@container">
      <div className="grid gap-4 @4xl:grid-cols-[1fr_18rem]">
        <WorkflowCanvas
          nodes={version.nodes}
          edges={version.edges}
          layout={layout}
          selection={selection}
          onSelect={setSelection}
        />
        <Inspector nodes={version.nodes} edges={version.edges} selection={selection} />
      </div>
    </div>
  );
}

export function WorkflowDetail() {
  const id = Number(useParams().workflowId);
  // Keyed so an error from one workflow's actions doesn't stay up on the next one opened.
  return <WorkflowDetailView key={id} id={id} />;
}

function WorkflowDetailView({ id }: { id: number }) {
  const navigate = useNavigate();
  const listHref = useListHref();
  const { reloadList } = useOutletContext<ListContext>();
  const workflow = useLoad(useCallback(() => api.getWorkflow(id), [id]));
  const versions = useLoad(useCallback(() => api.listWorkflowVersions(id), [id]));
  const runs = useLoad(useCallback(() => api.listWorkflowRuns(id), [id]));
  const [actionError, setActionError] = useState<string>();
  // The input for a new run, while the form for one is open.
  const [runInput, setRunInput] = useState<string>();
  const [starting, setStarting] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const buildRefusal = useRefusal("build");

  // Keep the list's statuses current while any run is still going, as the run page does (less
  // often while those left only wait for the user).
  usePoll(
    !!runs.data?.some((r) => isActive(r.status)),
    runs.reload,
    runs.data?.some((r) => r.status === "queued" || r.status === "running")
      ? undefined
      : WAITING_POLL_MS,
  );

  if (workflow.error) return <Placeholder>{workflow.error}</Placeholder>;
  if (!workflow.data) return null;
  const w = workflow.data;
  const current = w.current_version;

  const onArchived = async () => {
    setArchiving(false);
    setRunInput(undefined);
    await Promise.all([workflow.reload(), reloadList()]);
  };

  const onUnarchive = async () => {
    setActionError(undefined);
    try {
      await api.unarchiveWorkflow(id);
      await Promise.all([workflow.reload(), reloadList()]);
    } catch (err) {
      setActionError(describeError(err));
    }
  };

  const onStartRun = async (e: React.FormEvent) => {
    e.preventDefault();
    setStarting(true);
    setActionError(undefined);
    try {
      const run = await api.createWorkflowRun(id, runInput!);
      navigate(listHref(`/workflows/${id}/runs/${run.id}`));
    } catch (err) {
      setActionError(describeError(err));
    } finally {
      setStarting(false);
    }
  };

  // A workflow with an input schema starts from a form made from it.
  const onStartStructured = async (input: JsonValue) => {
    setStarting(true);
    setActionError(undefined);
    try {
      const run = await api.createWorkflowRun(id, { json: input });
      navigate(listHref(`/workflows/${id}/runs/${run.id}`));
    } catch (err) {
      setActionError(describeError(err));
    } finally {
      setStarting(false);
    }
  };

  return (
    <DetailPage
      wide
      title={
        <span className="flex items-center gap-2">
          {w.name} <VersionBadge version={current.version} />
          {w.archived_at && <ArchivedBadge />}
        </span>
      }
      subtitle={
        <>
          {w.description || `Updated ${formatDate(w.updated_at)}`}
          <ProvenanceLine of={w} className="mt-0.5" />
        </>
      }
      actions={
        <>
          {w.archived_at ? (
            !buildRefusal && (
              <Button size="sm" onClick={onUnarchive}>
                <ArchiveRestore />
                Unarchive
              </Button>
            )
          ) : (
            <Button
              size="sm"
              onClick={() => setRunInput(runInput === undefined ? "" : undefined)}
              disabled={current.nodes.length === 0}
            >
              <Play />
              Run
            </Button>
          )}
          {!buildRefusal && <ExportLink href={api.workflowExportUrl(id)} />}
          {!buildRefusal && (
            <Button asChild variant="outline" size="sm">
              <Link to={listHref(`/workflows/${id}/edit`)}>
                <Pencil />
                Edit
              </Link>
            </Button>
          )}
          {!w.archived_at && !buildRefusal && (
            <Button variant="outline" size="sm" onClick={() => setArchiving(true)}>
              <Archive />
              Archive
            </Button>
          )}
        </>
      }
    >
      <ErrorBanner message={actionError} onDismiss={() => setActionError(undefined)} />
      <RoleNote refusal={buildRefusal} />
      <UnrunnableNotice reason={current.unrunnable} />
      {w.archived_at && (
        <p role="status" className="bg-muted/50 rounded-md border px-3 py-2 text-sm">
          This workflow is archived{w.archived_by ? ` by ${personName(w.archived_by)}` : ""} on{" "}
          {formatDate(w.archived_at)}. It starts nothing new until it's unarchived, nor does a
          workflow that nests it; its schedules stay paused until someone resumes them.
        </p>
      )}
      {archiving && (
        <WorkflowArchiveDialog
          item={w}
          onClose={() => setArchiving(false)}
          onArchived={onArchived}
        />
      )}
      {runInput !== undefined && current.input_schema !== null && current.input_schema !== undefined && (
        <SchemaInputForm
          schema={current.input_schema}
          submitting={starting}
          onSubmit={onStartStructured}
          onCancel={() => setRunInput(undefined)}
        />
      )}
      {runInput !== undefined && (current.input_schema === null || current.input_schema === undefined) && (
        <form className="flex flex-col gap-2 rounded-md border p-4" onSubmit={onStartRun}>
          <label htmlFor="run-input" className="text-sm font-medium">
            Input
          </label>
          <Textarea
            id="run-input"
            rows={3}
            autoFocus
            value={runInput}
            onChange={(e) => setRunInput(e.target.value)}
            placeholder="What the first nodes are asked to do"
          />
          <p className="text-muted-foreground text-xs">
            Given to each start node; every other node gets the outputs of the nodes before it.
          </p>
          <div className="flex gap-2">
            <Button type="submit" size="sm" disabled={starting || !runInput.trim()}>
              Start run
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setRunInput(undefined)}>
              Cancel
            </Button>
          </div>
        </form>
      )}
      <Tabs
        label="Workflow details"
        tabs={[
          {
            id: "graph",
            label: "Graph",
            content: (
              // Keyed so the selection resets when the graph changes under it.
              <GraphView
                key={current.id}
                version={current}
                layout={w.layout}
                editHref={listHref(`/workflows/${id}/edit`)}
              />
            ),
          },
          {
            id: "runs",
            label: "Runs",
            count: runs.data?.length,
            content: (
              <>
                {runs.error && <p className="text-destructive text-sm">{runs.error}</p>}
                {runs.data?.length === 0 && (
                  <p className="text-muted-foreground text-sm">
                    No runs yet. Start one to watch it go.
                  </p>
                )}
                {!!runs.data?.length && (
                  <ul aria-label="Runs" className="divide-y rounded-md border">
                    {runs.data.map((run) => (
                      <li key={run.id}>
                        <Link
                          to={listHref(`/workflows/${id}/runs/${run.id}`)}
                          className="hover:bg-accent flex items-center gap-3 px-3 py-2"
                        >
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm">{run.input}</span>
                            <span className="text-muted-foreground flex items-center gap-1 text-xs">
                              {formatDate(run.created_at)}
                              <StartedBy run={run} before="· " />
                              {run.chat_id !== null && (
                                <span
                                  className="flex min-w-0 items-center gap-1"
                                  data-testid="started-from-chat"
                                >
                                  · <MessagesSquare className="size-3.5 shrink-0" aria-hidden />
                                  <span className="truncate">
                                    Started from the chat {run.chat_name}
                                  </span>
                                </span>
                              )}
                            </span>
                          </span>
                          <VersionBadge version={run.workflow_version} />
                          <RunStatusBadge status={run.status} />
                        </Link>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ),
          },
          {
            id: "versions",
            label: "Versions",
            count: versions.data?.length,
            content: (
              <VersionHistory
                versions={versions.data}
                error={versions.error}
                current={current.version}
                href={(v) => listHref(`/workflows/${id}/versions/${v}`)}
                detail={(v) => {
                  const s = versions.data?.find((x) => x.version === v);
                  return plural(s?.node_count ?? 0, "node");
                }}
              />
            ),
          },
          {
            id: "schedule",
            label: "Schedule",
            content: <TargetSchedules kind="workflow" id={id} />,
          },
        ]}
      />
    </DetailPage>
  );
}

export function WorkflowVersionPage() {
  const id = Number(useParams().workflowId);
  const number = Number(useParams().version);
  return <WorkflowVersionView key={`${id}/${number}`} id={id} number={number} />;
}

function WorkflowVersionView({ id, number }: { id: number; number: number }) {
  const navigate = useNavigate();
  const listHref = useListHref();
  const { reloadList } = useOutletContext<ListContext>();
  const workflow = useLoad(useCallback(() => api.getWorkflow(id), [id]));
  const version = useLoad(useCallback(() => api.getWorkflowVersion(id, number), [id, number]));
  const [error, setError] = useState<string>();
  const savedBy = useSavedBy();
  const buildRefusal = useRefusal("build");

  const loadError = version.error ?? workflow.error;
  if (loadError) return <Placeholder>{loadError}</Placeholder>;
  if (!version.data || !workflow.data) return null;
  const v = version.data;
  const isCurrent = workflow.data.current_version.version === v.version;

  async function onRestore() {
    try {
      await api.restoreWorkflowVersion(id, number);
      await reloadList();
      navigate(listHref(`/workflows/${id}/versions`));
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <DetailPage
      wide
      title={
        <span className="flex items-center gap-2">
          <Link to={listHref(`/workflows/${id}/versions`)} className="hover:underline">
            {workflow.data.name}
          </Link>
          <VersionBadge version={v.version} />
          {isCurrent && <Badge variant="secondary">current</Badge>}
        </span>
      }
      subtitle={`${v.note ?? "No note"} · ${formatDate(v.created_at)}${savedBy(v.created_by)}`}
      actions={!isCurrent && !buildRefusal && <RestoreButton onRestore={onRestore} />}
    >
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      <Section title="Graph">
        {/* The layout is the workflow's latest; nodes it doesn't place are laid out for you. */}
        <GraphView version={v} layout={workflow.data.layout} />
      </Section>
    </DetailPage>
  );
}

type Guards = { invocable_by: InvocableBy; limits: LimitsDraft<(typeof RUN_LIMITS)[number]> };
const newGuards: Guards = { invocable_by: "everyone", limits: draftOf(RUN_LIMITS, {}) };

/** Builds a new workflow, or edits one: its name, description, graph, who may invoke it and
 * its limits. */
export function WorkflowEditorPage() {
  const params = useParams();
  const id = params.workflowId ? Number(params.workflowId) : undefined;
  // Keyed so switching workflows starts a fresh draft.
  return <WorkflowEditorView key={id ?? "new"} id={id} />;
}

function WorkflowEditorView({ id }: { id: number | undefined }) {
  const navigate = useNavigate();
  const listHref = useListHref();
  const { reloadList } = useOutletContext<ListContext>();
  const [meta, setMeta] = useState<{ name: string; description: string; note: string }>();
  const [draft, setDraft] = useState<Draft>();
  // What was loaded, as a save request, to tell whether there are changes to lose.
  const [loaded, setLoaded] = useState<string>();
  // The version being edited, if any.
  const [editing, setEditing] = useState<number>();
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const buildRefusal = useRefusal("build");
  const role = useRole();
  const agents = useLoad(api.listAgents);
  const workflows = useLoad(api.listWorkflows);
  const organisation = useLoad(api.getOrganisation);
  const ceiling = organisation.data?.limits.in_force;
  const loadError = agents.error ?? workflows.error;
  // Who may invoke it and its own caps, as typed, and as saved.
  const [guards, setGuards] = useState<Guards>(newGuards);
  const [loadedGuards, setLoadedGuards] = useState<string>(JSON.stringify(newGuards));
  const [savedLimits, setSavedLimits] = useState<Partial<Limits>>({});

  useEffect(() => {
    const load = (meta: { name: string; description: string; note: string }, draft: Draft) => {
      setMeta(meta);
      setDraft(draft);
      setLoaded(JSON.stringify(toInput(draft, meta)));
    };
    if (id === undefined) {
      load({ name: "", description: "", note: "" }, { nodes: [], edges: [], positions: {} });
      return;
    }
    api.getWorkflow(id).then(
      (w) => {
        load(
          { name: w.name, description: w.description, note: "" },
          fromVersion(w.current_version, w.layout),
        );
        const saved = { invocable_by: w.invocable_by, limits: draftOf(RUN_LIMITS, w.limits) };
        setGuards(saved);
        setLoadedGuards(JSON.stringify(saved));
        setSavedLimits(w.limits);
        setEditing(w.current_version.version);
      },
      (err) => setError(describeError(err)),
    );
  }, [id]);

  const targets = useMemo<Target[] | undefined>(
    () =>
      agents.data &&
      workflows.data && [
        ...agents.data.map((a) => ({ kind: "agent" as const, ...pick(a) })),
        // A workflow can't contain itself, even through another workflow.
        ...workflows.data.map((w) => ({
          kind: "workflow" as const,
          ...pick(w),
          unavailable:
            w.id === id
              ? ("Current workflow" as const)
              : id !== undefined && w.contains.includes(id)
                ? ("Runs this workflow" as const)
                : undefined,
        })),
      ],
    [agents.data, workflows.data, id],
  );

  const dirty =
    !!meta && !!draft && (JSON.stringify(toInput(draft, meta)) !== loaded || JSON.stringify(guards) !== loadedGuards);
  // Set once saved or cancelled (which asks itself), so leaving then doesn't ask (again).
  const done = useRef(false);
  // Closing or reloading the tab asks first, and so does leaving within the app.
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  const blocker = useBlocker(() => dirty && !done.current);
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    if (window.confirm("Discard your unsaved changes?")) blocker.proceed();
    else blocker.reset();
  }, [blocker]);

  if (!meta || !draft) return error ? <Placeholder>{error}</Placeholder> : null;
  const set = (patch: Partial<typeof meta>) => setMeta({ ...meta, ...patch });
  const back = () => {
    if (dirty && !window.confirm("Discard your unsaved changes?")) return;
    done.current = true;
    navigate(listHref(id === undefined ? "/workflows" : `/workflows/${id}`));
  };

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    // Refused if someone saved another version meanwhile, rather than silently overwriting it.
    const body = {
      ...toInput(draft!, meta!),
      base_version: editing,
      invocable_by: guards.invocable_by,
      limits: limitsOf(RUN_LIMITS, guards.limits)!,
    };
    try {
      const saved =
        id === undefined ? await api.createWorkflow(body) : await api.updateWorkflow(id, body);
      await reloadList();
      done.current = true;
      navigate(listHref(`/workflows/${saved.id}`));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DetailPage
      wide
      title={id === undefined ? "New workflow" : "Edit workflow"}
      subtitle={
        editing !== undefined &&
        `Editing v${editing}. Changing nodes or edges (including a node's key) creates v${editing + 1}; renaming the workflow or moving nodes doesn't.`
      }
    >
      <form className="flex flex-col gap-5" onSubmit={onSubmit}>
        <RoleNote refusal={buildRefusal} />
        <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
        {/* Without these there's nothing to add, so say why. */}
        <ErrorBanner message={loadError && `Couldn't load agents and workflows: ${loadError}`} />
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Name" htmlFor="workflow-name">
            <Input
              id="workflow-name"
              required
              maxLength={255}
              value={meta.name}
              onChange={(e) => set({ name: e.target.value })}
              placeholder="e.g. Weekly report"
            />
          </Field>
          <Field label="Description" htmlFor="workflow-description">
            <Input
              id="workflow-description"
              value={meta.description}
              onChange={(e) => set({ description: e.target.value })}
            />
          </Field>
        </div>
        <Section title="Graph">
          <GraphEditor draft={draft} onChange={setDraft} targets={targets} />
        </Section>
        <Section title="Input and output">
          <div className="grid gap-4 md:grid-cols-2">
            <JsonField
              label="Input schema"
              value={draft.input_schema}
              onChange={(input_schema) => setDraft(setSchemas(draft, { input_schema }))}
              placeholder='{"type": "object", "properties": {"topic": {"type": "string"}}}'
              hint="JSON Schema. Runs whose input doesn't fit are refused, and the start form asks for its fields. Blank: any text."
            />
            <JsonField
              label="Output schema"
              value={draft.output_schema}
              onChange={(output_schema) => setDraft(setSchemas(draft, { output_schema }))}
              hint="JSON Schema the run's output is checked against: one final node's output, or an object of several's by node."
            />
          </div>
        </Section>
        <GuardFields
          kind="workflow"
          names={RUN_LIMITS}
          invocableBy={guards.invocable_by}
          onInvocableBy={(invocable_by) => setGuards({ ...guards, invocable_by })}
          draft={guards.limits}
          onDraft={(limits) => setGuards({ ...guards, limits })}
          ceiling={ceiling}
          saved={savedLimits}
          role={role}
        />
        {id !== undefined && (
          <Field
            label="Change note"
            htmlFor="workflow-note"
            hint="Optional. Shown in the version history."
          >
            <Input
              id="workflow-note"
              value={meta.note}
              onChange={(e) => set({ note: e.target.value })}
              placeholder="What changed and why"
            />
          </Field>
        )}
        <div className="flex gap-2">
          <Button
            type="submit"
            disabled={
              saving ||
              !meta.name.trim() ||
              !!buildRefusal ||
              !guardsValid(RUN_LIMITS, guards.limits, ceiling, savedLimits, role)
            }
            title={buildRefusal ?? undefined}
          >
            {id === undefined ? "Create workflow" : "Save changes"}
          </Button>
          <Button type="button" variant="ghost" onClick={back}>
            Cancel
          </Button>
        </div>
      </form>
    </DetailPage>
  );
}

const pick = (t: { id: number; name: string; version: number; version_id: number }) => ({
  id: t.id,
  name: t.name,
  version: t.version,
  version_id: t.version_id,
});
