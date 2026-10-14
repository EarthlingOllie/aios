import { useCallback, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams } from "react-router";
import { History, Trash2 } from "lucide-react";

import {
  DetailPage,
  ErrorBanner,
  Field,
  formatDate,
  MasterDetail,
  Placeholder,
  Section,
  useLoad,
  type ListContext,
} from "@/components/crud";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { VersionBadge, VersionHistory } from "@/components/versions";
import { WorkflowCanvas, type Selection } from "@/components/workflow/canvas";
import { Inspector } from "@/components/workflow/inspector";
import { api, describeError, type NodePosition, type WorkflowVersion } from "@/lib/api";

const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;

export function WorkflowsSection() {
  const { data, error, reload } = useLoad(api.listWorkflows);
  return (
    <MasterDetail
      title="Workflows"
      basePath="/workflows"
      items={data}
      error={error}
      empty="No workflows yet. A workflow chains agents, and other workflows, into a graph."
      reloadList={reload}
      renderItem={(workflow) => (
        <>
          <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate font-medium">{workflow.name}</span>
            <VersionBadge version={workflow.version} />
          </div>
          <div className="text-muted-foreground truncate text-xs">
            {plural(workflow.node_count, "node")}
            {workflow.description && ` · ${workflow.description}`}
          </div>
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
}: {
  version: WorkflowVersion;
  layout: Record<string, NodePosition>;
}) {
  const [selection, setSelection] = useState<Selection | null>(null);
  if (version.nodes.length === 0) {
    return (
      <p className="text-muted-foreground rounded-md border p-6 text-center text-sm">
        This workflow has no nodes yet.
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
  const { reloadList } = useOutletContext<ListContext>();
  const workflow = useLoad(useCallback(() => api.getWorkflow(id), [id]));
  const versions = useLoad(useCallback(() => api.listWorkflowVersions(id), [id]));
  const [actionError, setActionError] = useState<string>();

  if (workflow.error) return <Placeholder>{workflow.error}</Placeholder>;
  if (!workflow.data) return null;
  const w = workflow.data;
  const current = w.current_version;

  const onDelete = async () => {
    if (!window.confirm(`Delete the workflow “${w.name}” and its versions?`)) return;
    setActionError(undefined);
    try {
      await api.deleteWorkflow(id);
      await reloadList();
      navigate("/workflows");
    } catch (err) {
      setActionError(describeError(err));
    }
  };

  return (
    <DetailPage
      wide
      title={
        <span className="flex items-center gap-2">
          {w.name} <VersionBadge version={current.version} />
        </span>
      }
      subtitle={w.description || `Updated ${formatDate(w.updated_at)}`}
      actions={
        <Button variant="outline" size="sm" onClick={onDelete} aria-label="Delete workflow">
          <Trash2 />
        </Button>
      }
    >
      <ErrorBanner message={actionError} onDismiss={() => setActionError(undefined)} />
      <Section title="Graph">
        {/* Keyed so the selection resets when the graph changes under it. */}
        <GraphView key={current.id} version={current} layout={w.layout} />
      </Section>
      <Section title="Version history">
        <VersionHistory
          versions={versions.data}
          current={current.version}
          href={(v) => `/workflows/${id}/versions/${v}`}
          detail={(v) => {
            const s = versions.data?.find((x) => x.version === v);
            return plural(s?.node_count ?? 0, "node");
          }}
        />
      </Section>
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
  const { reloadList } = useOutletContext<ListContext>();
  const workflow = useLoad(useCallback(() => api.getWorkflow(id), [id]));
  const version = useLoad(useCallback(() => api.getWorkflowVersion(id, number), [id, number]));
  const [error, setError] = useState<string>();

  const loadError = version.error ?? workflow.error;
  if (loadError) return <Placeholder>{loadError}</Placeholder>;
  if (!version.data || !workflow.data) return null;
  const v = version.data;
  const isCurrent = workflow.data.current_version.version === v.version;

  async function onRestore() {
    try {
      await api.restoreWorkflowVersion(id, number);
      await reloadList();
      navigate(`/workflows/${id}`);
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <DetailPage
      wide
      title={
        <span className="flex items-center gap-2">
          <Link to={`/workflows/${id}`} className="hover:underline">
            {workflow.data.name}
          </Link>
          <VersionBadge version={v.version} />
          {isCurrent && <Badge variant="secondary">current</Badge>}
        </span>
      }
      subtitle={`${v.note ?? "No note"} · ${formatDate(v.created_at)}`}
      actions={
        !isCurrent && (
          <Button size="sm" variant="outline" onClick={onRestore}>
            <History />
            Restore this version
          </Button>
        )
      }
    >
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      <Section title="Graph">
        {/* The layout is the workflow's latest; nodes it doesn't place are laid out for you. */}
        <GraphView version={v} layout={workflow.data.layout} />
      </Section>
    </DetailPage>
  );
}

/** Names a new, empty workflow; its graph is built on its page. */
export function NewWorkflowForm() {
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    try {
      const saved = await api.createWorkflow({ name, description, nodes: [], edges: [] });
      await reloadList();
      navigate(`/workflows/${saved.id}`);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DetailPage title="New workflow">
      <form className="flex flex-col gap-5" onSubmit={onSubmit}>
        <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
        <Field label="Name" htmlFor="workflow-name">
          <Input
            id="workflow-name"
            required
            maxLength={255}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Weekly report"
          />
        </Field>
        <Field label="Description" htmlFor="workflow-description">
          <Input
            id="workflow-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        <div className="flex gap-2">
          <Button type="submit" disabled={saving || !name.trim()}>
            Create workflow
          </Button>
          <Button type="button" variant="ghost" onClick={() => navigate(-1)}>
            Cancel
          </Button>
        </div>
      </form>
    </DetailPage>
  );
}
