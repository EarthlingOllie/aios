import { useCallback, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams } from "react-router";
import { ArrowRight, Bot, Trash2, Workflow as WorkflowIcon } from "lucide-react";

import {
  DetailPage,
  ErrorBanner,
  formatDate,
  MasterDetail,
  Placeholder,
  Section,
  useLoad,
  type ListContext,
} from "@/components/crud";
import { TargetSchedules } from "@/components/schedules";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { VersionBadge } from "@/components/versions";
import { api, describeError } from "@/lib/api";

// Workflows are built through the API for now; these pages show them and their schedules.

export function WorkflowsSection() {
  const { data, error, reload } = useLoad(api.listWorkflows);
  return (
    <MasterDetail
      title="Workflows"
      basePath="/workflows"
      items={data}
      error={error}
      empty="No workflows yet. Workflows chain agents into graphs, and can reuse other workflows."
      reloadList={reload}
      canCreate={false}
      renderItem={(w) => (
        <>
          <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate font-medium">{w.name}</span>
            <VersionBadge version={w.version} />
          </div>
          <div className="text-muted-foreground truncate text-xs">
            {w.node_count} step{w.node_count === 1 ? "" : "s"}
          </div>
        </>
      )}
    />
  );
}

export function WorkflowsIndex() {
  return <Placeholder>Select a workflow.</Placeholder>;
}

export function WorkflowDetail() {
  const id = Number(useParams().workflowId);
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const workflow = useLoad(useCallback(() => api.getWorkflow(id), [id]));
  const [error, setError] = useState<string>();

  if (workflow.error) return <Placeholder>{workflow.error}</Placeholder>;
  if (!workflow.data) return null;
  const w = workflow.data;
  const v = w.current_version;

  const onDelete = async () => {
    if (!window.confirm(`Delete the workflow “${w.name}” and its schedules?`)) return;
    try {
      await api.deleteWorkflow(id);
      await reloadList();
      navigate("/workflows");
    } catch (err) {
      setError(describeError(err));
    }
  };

  return (
    <DetailPage
      title={
        <span className="flex items-center gap-2">
          {w.name} <VersionBadge version={v.version} />
        </span>
      }
      subtitle={w.description || `Updated ${formatDate(w.updated_at)}`}
      actions={
        <Button variant="outline" size="sm" onClick={onDelete} aria-label="Delete workflow">
          <Trash2 />
        </Button>
      }
    >
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      <Section title="Steps">
        {v.nodes.length === 0 ? (
          <p className="text-muted-foreground text-sm">No steps.</p>
        ) : (
          <ul className="divide-y rounded-md border">
            {v.nodes.map((n) => {
              const Icon = n.kind === "agent" ? Bot : WorkflowIcon;
              const href = `/${n.kind === "agent" ? "agents" : "workflows"}/${n.target_id}`;
              return (
                <li key={n.key} className="flex items-center gap-2 px-3 py-2 text-sm">
                  <code className="bg-muted rounded px-1 text-xs">{n.key}</code>
                  <Icon className="text-muted-foreground size-4" />
                  <Link to={href} className="font-medium hover:underline">
                    {n.name}
                  </Link>
                  <VersionBadge version={n.version} />
                  {n.latest_version === null && <Badge variant="secondary">deleted</Badge>}
                </li>
              );
            })}
          </ul>
        )}
        {v.edges.length > 0 && (
          <ul className="text-muted-foreground flex flex-wrap gap-x-4 gap-y-1 text-xs">
            {v.edges.map((e) => (
              <li key={`${e.source}>${e.target}`} className="flex items-center gap-1">
                <code>{e.source}</code>
                <ArrowRight className="size-3" />
                <code>{e.target}</code>
              </li>
            ))}
          </ul>
        )}
      </Section>
      <TargetSchedules kind="workflow" id={id} />
    </DetailPage>
  );
}
