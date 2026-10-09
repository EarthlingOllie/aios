import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams } from "react-router";
import { MessageSquare, Pencil, Play, Trash2 } from "lucide-react";

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
import { Textarea } from "@/components/ui/textarea";
import { api, describeError, type AgentInput, type SkillSummary } from "@/lib/api";

export function AgentsSection() {
  const { data, error, reload } = useLoad(api.listAgents);
  return (
    <MasterDetail
      title="Agents"
      basePath="/agents"
      items={data}
      error={error}
      empty="No agents yet. An agent is a base prompt plus a set of skills."
      reloadList={reload}
      renderItem={(agent) => (
        <>
          <div className="truncate font-medium">{agent.name}</div>
          <div className="text-muted-foreground truncate text-xs">
            {agent.skill_count} skill{agent.skill_count === 1 ? "" : "s"} · {agent.run_count} run
            {agent.run_count === 1 ? "" : "s"}
          </div>
        </>
      )}
    />
  );
}

export function AgentsIndex() {
  return <Placeholder>Select an agent, or create a new one.</Placeholder>;
}

export function AgentDetail() {
  const id = Number(useParams().agentId);
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const { data: agent, error } = useLoad(useCallback(() => api.getAgent(id), [id]));
  const runs = useLoad(useCallback(() => api.listRuns(id), [id]));
  const [actionError, setActionError] = useState<string>();
  const [starting, setStarting] = useState(false);

  if (error) return <Placeholder>{error}</Placeholder>;
  if (!agent) return null;

  async function onDelete() {
    if (!window.confirm(`Delete the agent “${agent!.name}” and all of its runs?`)) return;
    try {
      await api.deleteAgent(id);
      await reloadList();
      navigate("/agents");
    } catch (err) {
      setActionError(describeError(err));
    }
  }

  async function onNewRun() {
    setStarting(true);
    try {
      const run = await api.createRun(id);
      reloadList();
      navigate(`runs/${run.id}`);
    } catch (err) {
      setActionError(describeError(err));
    } finally {
      setStarting(false);
    }
  }

  async function onDeleteRun(runId: number) {
    if (!window.confirm("Delete this run and its history?")) return;
    try {
      await api.deleteRun(id, runId);
      await Promise.all([runs.reload(), reloadList()]);
    } catch (err) {
      setActionError(describeError(err));
    }
  }

  return (
    <DetailPage
      title={agent.name}
      subtitle={agent.description || `Updated ${formatDate(agent.updated_at)}`}
      actions={
        <>
          <Button size="sm" onClick={onNewRun} disabled={starting}>
            <Play />
            New run
          </Button>
          <Button asChild variant="outline" size="sm">
            <Link to="edit">
              <Pencil />
              Edit
            </Link>
          </Button>
          <Button variant="outline" size="sm" onClick={onDelete} aria-label="Delete agent">
            <Trash2 />
          </Button>
        </>
      }
    >
      <ErrorBanner message={actionError} onDismiss={() => setActionError(undefined)} />

      <Section title="Base prompt">
        {agent.base_prompt ? (
          <pre className="bg-muted/50 rounded-md border p-3 text-sm whitespace-pre-wrap">
            {agent.base_prompt}
          </pre>
        ) : (
          <p className="text-muted-foreground text-sm">No base prompt; a generic one is used.</p>
        )}
      </Section>

      <Section title="Skills">
        {agent.skills.length === 0 ? (
          <p className="text-muted-foreground text-sm">No skills attached.</p>
        ) : (
          <ul className="divide-y rounded-md border">
            {agent.skills.map((skill) => (
              <li key={skill.id}>
                <Link to={`/skills/${skill.id}`} className="hover:bg-accent block px-3 py-2">
                  <div className="text-sm font-medium">{skill.name}</div>
                  {skill.description && (
                    <div className="text-muted-foreground truncate text-xs">
                      {skill.description}
                    </div>
                  )}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <details className="group">
        <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs font-semibold tracking-wide uppercase">
          Instructions sent to the model
        </summary>
        <pre className="bg-muted/50 mt-2 max-h-96 overflow-y-auto rounded-md border p-3 text-xs whitespace-pre-wrap">
          {agent.instructions}
        </pre>
      </details>

      <Section title="Runs">
        {runs.error && <p className="text-destructive text-sm">{runs.error}</p>}
        {runs.data?.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No runs yet. Start one to chat with this agent.
          </p>
        )}
        {!!runs.data?.length && (
          <ul className="divide-y rounded-md border">
            {runs.data.map((run) => (
              <li key={run.id} className="group flex items-center gap-2 pr-2">
                <Link
                  to={`runs/${run.id}`}
                  className="hover:bg-accent flex min-w-0 flex-1 items-center gap-3 px-3 py-2"
                >
                  <MessageSquare className="text-muted-foreground size-4 shrink-0" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm">
                      {run.title ?? <span className="text-muted-foreground">Untitled run</span>}
                    </span>
                    <span className="text-muted-foreground block text-xs">
                      {formatDate(run.updated_at)}
                    </span>
                  </span>
                  <Badge variant="secondary">{run.message_count} msgs</Badge>
                </Link>
                <button
                  className="text-muted-foreground hover:text-destructive focus:opacity-100 md:opacity-0 md:group-hover:opacity-100"
                  onClick={() => onDeleteRun(run.id)}
                  aria-label="Delete run"
                >
                  <Trash2 className="size-4" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </DetailPage>
  );
}

const emptyAgent: AgentInput = { name: "", description: "", base_prompt: "", skill_ids: [] };

export function AgentForm() {
  const params = useParams();
  const id = params.agentId ? Number(params.agentId) : undefined;
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const [form, setForm] = useState<AgentInput>();
  const [skills, setSkills] = useState<SkillSummary[]>();
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setForm(undefined);
    api.listSkills().then(setSkills, (err) => setError(describeError(err)));
    if (id === undefined) setForm(emptyAgent);
    else
      api.getAgent(id).then(
        (a) =>
          setForm({
            name: a.name,
            description: a.description,
            base_prompt: a.base_prompt,
            skill_ids: a.skills.map((s) => s.id),
          }),
        (err) => setError(describeError(err)),
      );
  }, [id]);

  if (!form) return error ? <Placeholder>{error}</Placeholder> : null;
  const set = (patch: Partial<AgentInput>) => setForm({ ...form, ...patch });
  const toggleSkill = (skillId: number, on: boolean) =>
    set({
      skill_ids: on
        ? [...form.skill_ids, skillId]
        : form.skill_ids.filter((existing) => existing !== skillId),
    });

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    try {
      const saved = id === undefined ? await api.createAgent(form!) : await api.updateAgent(id, form!);
      await reloadList();
      navigate(`/agents/${saved.id}`);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DetailPage title={id === undefined ? "New agent" : "Edit agent"}>
      <form className="flex flex-col gap-5" onSubmit={onSubmit}>
        <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
        <Field label="Name" htmlFor="agent-name">
          <Input
            id="agent-name"
            required
            maxLength={255}
            value={form.name}
            onChange={(e) => set({ name: e.target.value })}
            placeholder="e.g. Support triage"
          />
        </Field>
        <Field label="Description" htmlFor="agent-description" hint="For you; not sent to the model.">
          <Input
            id="agent-description"
            value={form.description}
            onChange={(e) => set({ description: e.target.value })}
          />
        </Field>
        <Field
          label="Base prompt"
          htmlFor="agent-prompt"
          hint="The agent's core instructions. Attached skills are appended after it."
        >
          <Textarea
            id="agent-prompt"
            rows={10}
            className="font-mono text-sm"
            value={form.base_prompt}
            onChange={(e) => set({ base_prompt: e.target.value })}
            placeholder="You are a support engineer. Classify each ticket…"
          />
        </Field>
        <fieldset className="flex flex-col gap-1.5">
          <legend className="mb-1.5 text-sm font-medium">Skills</legend>
          {skills?.length === 0 && (
            <p className="text-muted-foreground text-sm">
              You have no skills yet.{" "}
              <Link to="/skills/new" className="underline">
                Create one
              </Link>
              .
            </p>
          )}
          {!!skills?.length && (
            <ul className="divide-y rounded-md border">
              {skills.map((skill) => (
                <li key={skill.id}>
                  <label className="hover:bg-accent flex cursor-pointer items-start gap-3 px-3 py-2">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={form.skill_ids.includes(skill.id)}
                      onChange={(e) => toggleSkill(skill.id, e.target.checked)}
                    />
                    <span className="min-w-0">
                      <span className="block text-sm font-medium">{skill.name}</span>
                      {skill.description && (
                        <span className="text-muted-foreground block truncate text-xs">
                          {skill.description}
                        </span>
                      )}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </fieldset>
        <div className="flex gap-2">
          <Button type="submit" disabled={saving || !form.name.trim()}>
            {id === undefined ? "Create agent" : "Save changes"}
          </Button>
          <Button type="button" variant="ghost" onClick={() => navigate(-1)}>
            Cancel
          </Button>
        </div>
      </form>
    </DetailPage>
  );
}
