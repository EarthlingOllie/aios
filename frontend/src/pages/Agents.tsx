import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams } from "react-router";
import { ArrowUpCircle, History, MessageSquare, Pencil, Play, Trash2 } from "lucide-react";

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
import { ToolChanges, ToolList, ToolSettingsFields } from "@/components/tools";
import { CompareVersions, VersionBadge, VersionHistory } from "@/components/versions";
import {
  api,
  describeError,
  type Agent,
  type AgentInput,
  type AgentSkill,
  type AgentVersion,
  type SkillSummary,
  type ToolInfo,
} from "@/lib/api";

export function AgentsSection() {
  const { data, error, reload } = useLoad(api.listAgents);
  return (
    <MasterDetail
      title="Agents"
      basePath="/agents"
      items={data}
      error={error}
      empty="No agents yet. An agent is a base prompt plus a set of skills and tools."
      reloadList={reload}
      renderItem={(agent) => (
        <>
          <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate font-medium">{agent.name}</span>
            <VersionBadge version={agent.version} />
          </div>
          <div className="text-muted-foreground truncate text-xs">
            {agent.skill_count} skill{agent.skill_count === 1 ? "" : "s"} · {agent.tool_count} tool
            {agent.tool_count === 1 ? "" : "s"} · {agent.run_count} run
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

/** The agent as an update body, with optional changes. */
function agentInput(agent: Agent, changes: Partial<AgentInput> = {}): AgentInput {
  const v = agent.current_version;
  return {
    name: agent.name,
    description: agent.description,
    base_prompt: v.base_prompt,
    skill_version_ids: v.skills.map((s) => s.skill_version_id),
    tools: v.tools.map(({ key, config }) => ({ key, config })),
    ...changes,
  };
}

function SkillRow({ skill, action }: { skill: AgentSkill; action?: React.ReactNode }) {
  return (
    <li className="flex items-center gap-2 pr-2">
      <Link to={`/skills/${skill.skill_id}`} className="hover:bg-accent min-w-0 flex-1 px-3 py-2">
        <span className="flex items-center gap-2">
          <span className="text-sm font-medium">{skill.name}</span>
          <VersionBadge version={skill.version} />
          {skill.latest_version === null && <Badge variant="secondary">deleted</Badge>}
        </span>
        {skill.description && (
          <span className="text-muted-foreground block truncate text-xs">{skill.description}</span>
        )}
      </Link>
      {action}
    </li>
  );
}

function VersionConfig({ version }: { version: AgentVersion }) {
  return (
    <>
      <Section title="Base prompt">
        {version.base_prompt ? (
          <pre className="bg-muted/50 rounded-md border p-3 text-sm whitespace-pre-wrap">
            {version.base_prompt}
          </pre>
        ) : (
          <p className="text-muted-foreground text-sm">No base prompt; a generic one is used.</p>
        )}
      </Section>
      <details className="group">
        <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs font-semibold tracking-wide uppercase">
          Instructions sent to the model
        </summary>
        <pre className="bg-muted/50 mt-2 max-h-96 overflow-y-auto rounded-md border p-3 text-xs whitespace-pre-wrap">
          {version.instructions}
        </pre>
      </details>
    </>
  );
}

export function AgentDetail() {
  const id = Number(useParams().agentId);
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const agent = useLoad(useCallback(() => api.getAgent(id), [id]));
  const versions = useLoad(useCallback(() => api.listAgentVersions(id), [id]));
  const runs = useLoad(useCallback(() => api.listRuns(id), [id]));
  const catalog = useLoad(api.listTools);
  const [actionError, setActionError] = useState<string>();
  const [busy, setBusy] = useState(false);

  if (agent.error) return <Placeholder>{agent.error}</Placeholder>;
  if (!agent.data) return null;
  const a = agent.data;
  const current = a.current_version;

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setActionError(undefined);
    try {
      await fn();
    } catch (err) {
      setActionError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  const onDelete = () => {
    if (!window.confirm(`Delete the agent “${a.name}”, its versions and all of its runs?`)) return;
    act(async () => {
      await api.deleteAgent(id);
      await reloadList();
      navigate("/agents");
    });
  };

  const onNewRun = () =>
    act(async () => {
      const run = await api.createRun(id);
      reloadList();
      navigate(`runs/${run.id}`);
    });

  const onUpdateSkill = (skill: AgentSkill) => {
    // The update keeps the current tools, which the server rejects if any left the library.
    // Dropping them here would change the agent unasked, so send the user to the edit form.
    const retired = current.tools.filter((t) => !t.available).map((t) => t.name);
    if (retired.length) {
      const one = retired.length === 1;
      setActionError(
        `Can't update ${skill.name}: ${retired.join(", ")} ${one ? "is" : "are"} no longer ` +
          `available. Use Edit to remove ${one ? "it" : "them"} first.`,
      );
      return;
    }
    act(async () => {
      const ids = current.skills.map((s) =>
        s.skill_id === skill.skill_id ? skill.latest_version_id! : s.skill_version_id,
      );
      await api.updateAgent(
        id,
        agentInput(a, {
          skill_version_ids: ids,
          note: `Update ${skill.name} to v${skill.latest_version}`,
        }),
      );
      await Promise.all([agent.reload(), versions.reload(), reloadList()]);
    });
  };

  const onDeleteRun = (runId: number) => {
    if (!window.confirm("Delete this run and its history?")) return;
    act(async () => {
      await api.deleteRun(id, runId);
      await Promise.all([runs.reload(), versions.reload(), reloadList()]);
    });
  };

  return (
    <DetailPage
      title={
        <span className="flex items-center gap-2">
          {a.name} <VersionBadge version={current.version} />
        </span>
      }
      subtitle={a.description || `Updated ${formatDate(a.updated_at)}`}
      actions={
        <>
          <Button size="sm" onClick={onNewRun} disabled={busy}>
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

      <Section title="Skills">
        {current.skills.length === 0 ? (
          <p className="text-muted-foreground text-sm">No skills attached.</p>
        ) : (
          <ul className="divide-y rounded-md border">
            {current.skills.map((skill) => (
              <SkillRow
                key={skill.skill_id}
                skill={skill}
                action={
                  skill.latest_version !== null &&
                  skill.latest_version > skill.version && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() => onUpdateSkill(skill)}
                    >
                      <ArrowUpCircle />
                      Update to v{skill.latest_version}
                    </Button>
                  )
                }
              />
            ))}
          </ul>
        )}
      </Section>

      <Section title="Tools">
        <ToolList tools={current.tools} catalog={catalog.data} />
      </Section>

      <VersionConfig version={current} />

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
                  <VersionBadge version={run.agent_version} />
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

      <Section title="Version history">
        <VersionHistory
          versions={versions.data}
          current={current.version}
          href={(v) => `/agents/${id}/versions/${v}`}
          detail={(v) => {
            const s = versions.data?.find((x) => x.version === v);
            const count = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;
            return [
              count(s?.skill_count ?? 0, "skill"),
              count(s?.tool_count ?? 0, "tool"),
              count(s?.run_count ?? 0, "run"),
            ].join(" · ");
          }}
        />
      </Section>
    </DetailPage>
  );
}

export function AgentVersionPage() {
  const id = Number(useParams().agentId);
  const number = Number(useParams().version);
  // Keyed so per-version state (e.g. the chosen comparison base) resets when
  // navigating to another version.
  return <AgentVersionView key={`${id}/${number}`} id={id} number={number} />;
}

function AgentVersionView({ id, number }: { id: number; number: number }) {
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const agent = useLoad(useCallback(() => api.getAgent(id), [id]));
  const version = useLoad(useCallback(() => api.getAgentVersion(id, number), [id, number]));
  const versions = useLoad(useCallback(() => api.listAgentVersions(id), [id]));
  const catalog = useLoad(api.listTools);
  const older = (versions.data ?? []).map((v) => v.version).filter((v) => v < number);
  const [base, setBase] = useState<number>();
  const baseVersion = base ?? older[0];
  const before = useLoad(
    useCallback(
      () =>
        baseVersion === undefined
          ? Promise.resolve(undefined)
          : api.getAgentVersion(id, baseVersion),
      [id, baseVersion],
    ),
  );
  const [error, setError] = useState<string>();

  if (version.error) return <Placeholder>{version.error}</Placeholder>;
  if (!version.data || !agent.data) return null;
  const v = version.data;
  const isCurrent = agent.data.current_version.version === v.version;

  async function onRestore() {
    try {
      await api.restoreAgentVersion(id, number);
      await reloadList();
      navigate(`/agents/${id}`);
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <DetailPage
      title={
        <span className="flex items-center gap-2">
          <Link to={`/agents/${id}`} className="hover:underline">
            {agent.data.name}
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
      <Section title="Skills">
        {v.skills.length === 0 ? (
          <p className="text-muted-foreground text-sm">No skills.</p>
        ) : (
          <ul className="divide-y rounded-md border">
            {v.skills.map((skill) => (
              <SkillRow key={skill.skill_id} skill={skill} />
            ))}
          </ul>
        )}
      </Section>
      <Section title="Tools">
        <ToolList tools={v.tools} catalog={catalog.data} />
      </Section>
      <VersionConfig version={v} />
      <Section title="Changes to the instructions">
        <CompareVersions
          versions={older}
          base={baseVersion}
          onBaseChange={setBase}
          before={before.data?.instructions}
          after={v.instructions}
        />
      </Section>
      {baseVersion !== undefined && before.data && (
        <Section title={`Changes to the tools since v${baseVersion}`}>
          <ToolChanges before={before.data.tools} after={v.tools} />
        </Section>
      )}
    </DetailPage>
  );
}

type FormState = Omit<AgentInput, "skill_version_ids"> & { skill_ids: number[] };

const emptyAgent: FormState = {
  name: "",
  description: "",
  base_prompt: "",
  skill_ids: [],
  tools: [],
};

export function AgentForm() {
  const params = useParams();
  const id = params.agentId ? Number(params.agentId) : undefined;
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const [form, setForm] = useState<FormState>();
  // The version being edited, if any.
  const [loaded, setLoaded] = useState<AgentVersion>();
  // Skills attached to the current version, by skill id: these stay on their pinned version.
  const [pinned, setPinned] = useState<Map<number, AgentSkill>>(new Map());
  const [skills, setSkills] = useState<SkillSummary[]>();
  const [catalog, setCatalog] = useState<ToolInfo[]>();
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setForm(undefined);
    setLoaded(undefined);
    setPinned(new Map());
    api.listSkills().then(setSkills, (err) => setError(describeError(err)));
    api.listTools().then(setCatalog, (err) => setError(describeError(err)));
    if (id === undefined) setForm(emptyAgent);
    else
      api.getAgent(id).then(
        (a) => {
          const v = a.current_version;
          setForm({
            name: a.name,
            description: a.description,
            base_prompt: v.base_prompt,
            skill_ids: v.skills.map((s) => s.skill_id),
            tools: v.tools.map(({ key, config }) => ({ key, config })),
            note: "",
          });
          setLoaded(v);
          setPinned(new Map(v.skills.map((s) => [s.skill_id, s])));
        },
        (err) => setError(describeError(err)),
      );
  }, [id]);

  if (!form) return error ? <Placeholder>{error}</Placeholder> : null;
  const set = (patch: Partial<FormState>) => setForm({ ...form, ...patch });
  const toggleSkill = (skillId: number, on: boolean) =>
    set({
      skill_ids: on
        ? [...form.skill_ids, skillId]
        : form.skill_ids.filter((existing) => existing !== skillId),
    });
  const toggleTool = (tool: ToolInfo, on: boolean) =>
    set({
      tools: on
        ? [...form.tools, { key: tool.key, config: tool.default_config }]
        : form.tools.filter((t) => t.key !== tool.key),
    });
  const setToolConfig = (key: string, config: Record<string, unknown>) =>
    set({ tools: form.tools.map((t) => (t.key === key ? { ...t, config } : t)) });
  // Attached tools that have since left the library, so they can be removed. Unknown until the
  // catalog loads.
  const retired = catalog ? form.tools.filter((t) => !catalog.some((c) => c.key === t.key)) : [];
  const toolName = (key: string) => loaded?.tools.find((t) => t.key === key)?.name ?? key;

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    const latest = new Map(skills?.map((s) => [s.id, s.version_id]));
    const { skill_ids, ...rest } = form!;
    const body: AgentInput = {
      ...rest,
      note: rest.note?.trim() || undefined,
      // Already-attached skills keep their pinned version; newly added ones get the latest.
      skill_version_ids: skill_ids.map((s) => pinned.get(s)?.skill_version_id ?? latest.get(s)!),
    };
    try {
      const saved = id === undefined ? await api.createAgent(body) : await api.updateAgent(id, body);
      await reloadList();
      navigate(`/agents/${saved.id}`);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DetailPage
      title={id === undefined ? "New agent" : "Edit agent"}
      subtitle={
        loaded &&
        `Editing v${loaded.version}. Changing the prompt, skills or tools creates v${loaded.version + 1}; renaming doesn't. Existing runs stay on the version they started with.`
      }
    >
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
              {skills.map((skill) => {
                const pin = pinned.get(skill.id);
                return (
                  <li key={skill.id}>
                    <label className="hover:bg-accent flex cursor-pointer items-start gap-3 px-3 py-2">
                      <input
                        type="checkbox"
                        className="mt-1"
                        checked={form.skill_ids.includes(skill.id)}
                        onChange={(e) => toggleSkill(skill.id, e.target.checked)}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm font-medium">{skill.name}</span>
                        {skill.description && (
                          <span className="text-muted-foreground block truncate text-xs">
                            {skill.description}
                          </span>
                        )}
                      </span>
                      <VersionBadge version={pin?.version ?? skill.version} />
                      {pin && pin.version < skill.version && (
                        <span className="text-muted-foreground text-xs">latest v{skill.version}</span>
                      )}
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </fieldset>
        <fieldset className="flex flex-col gap-1.5">
          <legend className="mb-1.5 text-sm font-medium">Tools</legend>
          <ul className="divide-y rounded-md border">
            {catalog?.map((tool) => {
              const attached = form.tools.find((t) => t.key === tool.key);
              return (
                <li key={tool.key}>
                  <label className="hover:bg-accent flex cursor-pointer items-start gap-3 px-3 py-2">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={!!attached}
                      onChange={(e) => toggleTool(tool, e.target.checked)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium">{tool.name}</span>
                      <span className="text-muted-foreground block text-xs">{tool.description}</span>
                    </span>
                  </label>
                  {attached && (
                    <div className="px-3 pt-1 pb-3 pl-9">
                      <ToolSettingsFields
                        tool={tool}
                        value={attached.config}
                        onChange={(config) => setToolConfig(tool.key, config)}
                      />
                    </div>
                  )}
                </li>
              );
            })}
            {retired.map(({ key }) => (
              <li key={key}>
                <label className="hover:bg-accent flex cursor-pointer items-start gap-3 px-3 py-2">
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked
                    onChange={() => set({ tools: form.tools.filter((t) => t.key !== key) })}
                  />
                  <span className="min-w-0 flex-1 text-sm font-medium">{toolName(key)}</span>
                  <Badge variant="secondary">no longer available, remove to save</Badge>
                </label>
              </li>
            ))}
          </ul>
        </fieldset>
        {id !== undefined && (
          <Field label="Change note" htmlFor="agent-note" hint="Optional. Shown in the version history.">
            <Input
              id="agent-note"
              value={form.note ?? ""}
              onChange={(e) => set({ note: e.target.value })}
              placeholder="What changed and why"
            />
          </Field>
        )}
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
