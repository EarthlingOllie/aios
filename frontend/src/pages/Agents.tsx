import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams, useSearchParams } from "react-router";
import {
  Archive,
  ArchiveRestore,
  ArrowUpCircle,
  CalendarClock,
  MessageSquare,
  MessagesSquare,
  Pencil,
  Play,
  Trash2,
} from "lucide-react";

import {
  DetailPage,
  ErrorBanner,
  Field,
  formatDate,
  MasterDetail,
  Placeholder,
  Section,
  useListHref,
  useLoad,
  type ListContext,
} from "@/components/crud";
import { AgentArchiveDialog } from "@/components/ArchiveDialog";
import { ProvenanceLine, StartedBy, useSavedBy } from "@/components/provenance";
import { AgentRunStatusBadge, RunCountersLine } from "@/components/agent-runs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { MarkdownEditor } from "@/components/MarkdownEditor";
import { TargetSchedules } from "@/components/schedules";
import { Tabs } from "@/components/tabs";
import { ToolChanges, ToolList, ToolSettingsFields } from "@/components/tools";
import {
  CompareVersions,
  RestoreButton,
  VersionBadge,
  VersionHistory,
} from "@/components/versions";
import {
  api,
  describeError,
  type Agent,
  type AgentInput,
  type AgentSkill,
  type AgentSummary,
  type AgentVersion,
  type SkillSummary,
  type ToolInfo,
} from "@/lib/api";
import {
  byMostRuns,
  nameAndDescription,
  NAME_SORT,
  UPDATED_SORT,
  type ListFilterGroup,
  type ListSort,
} from "@/lib/list-filter";
import { personName } from "@/lib/provenance";

const AGENT_FILTERS: ListFilterGroup<AgentSummary>[] = [
  {
    param: "filter",
    label: "Show",
    all: "All",
    options: [
      { key: "used", label: "Used", test: (agent) => agent.run_count > 0 },
      { key: "unused", label: "Unused", test: (agent) => agent.run_count === 0 },
      // The list is loaded from the archived agents for this one (`AgentsSection`); the others
      // narrow the default list, which leaves archived agents out.
      { key: "archived", label: "Archived", test: (agent) => agent.archived_at !== null },
    ],
  },
];

const AGENT_SORTS: ListSort<AgentSummary>[] = [
  NAME_SORT,
  UPDATED_SORT,
  { key: "runs", label: "Most runs", compare: byMostRuns },
];

/**
 * The Agents list's filters: Used or Unused, and the select of a skill and of a tool, filled
 * from the user's skills and the tool library. Until those have loaded (or if they can't), the
 * skill or tool already in the URL (`chosen`) is the only option, so a link opens filtered
 * straight away; once they have, one that doesn't exist is as if none were chosen.
 */
function agentFilters(
  skills: SkillSummary[] | undefined,
  tools: ToolInfo[] | undefined,
  chosen: { skill: string | null; tool: string | null },
): ListFilterGroup<AgentSummary>[] {
  const choices = (loaded: { key: string; label: string }[] | undefined, key: string | null) =>
    loaded ?? (key ? [{ key, label: key }] : []);
  const skillChoices = choices(
    skills?.map((skill) => ({ key: String(skill.id), label: skill.name })),
    chosen.skill,
  );
  const toolChoices = choices(
    tools?.map((tool) => ({ key: tool.key, label: tool.name })),
    chosen.tool,
  );
  return [
    ...AGENT_FILTERS,
    {
      param: "skill",
      label: "Skill",
      all: "Any skill",
      kind: "select",
      options: skillChoices.map((choice) => ({
        ...choice,
        test: (agent) => agent.skill_ids.map(String).includes(choice.key),
      })),
    },
    {
      param: "tool",
      label: "Tool",
      all: "Any tool",
      kind: "select",
      options: toolChoices.map((choice) => ({
        ...choice,
        test: (agent) => agent.tool_keys.includes(choice.key),
      })),
    },
  ];
}

/** The badge an archived agent, skill or workflow carries, in its list and on its page. */
export function ArchivedBadge() {
  return <Badge variant="secondary">archived</Badge>;
}

export function AgentsSection() {
  const [params] = useSearchParams();
  const archived = params.get("filter") === "archived";
  const { data, error, reload } = useLoad(
    useCallback(() => api.listAgents(archived), [archived]),
  );
  const { data: skills } = useLoad(api.listSkills);
  const { data: tools } = useLoad(api.listTools);
  const skill = params.get("skill");
  const tool = params.get("tool");
  const filters = useMemo(
    () => agentFilters(skills, tools, { skill, tool }),
    [skills, tools, skill, tool],
  );
  return (
    <MasterDetail
      title="Agents"
      basePath="/agents"
      items={data}
      error={error}
      empty="No agents yet. An agent is a base prompt plus a set of skills and tools."
      reloadList={reload}
      search={nameAndDescription}
      filters={filters}
      sorts={AGENT_SORTS}
      renderItem={(agent) => (
        <>
          <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate font-medium">{agent.name}</span>
            {agent.archived_at && <ArchivedBadge />}
            <VersionBadge version={agent.version} />
          </div>
          <div className="text-muted-foreground truncate text-xs">
            {agent.skill_count} skill{agent.skill_count === 1 ? "" : "s"} · {agent.tool_count} tool
            {agent.tool_count === 1 ? "" : "s"} · {agent.run_count} run
            {agent.run_count === 1 ? "" : "s"}
          </div>
          <ProvenanceLine of={agent} />
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
          {skill.archived && <Badge variant="secondary">archived</Badge>}
        </span>
        {skill.description && (
          <span className="text-muted-foreground block truncate text-xs">{skill.description}</span>
        )}
      </Link>
      {action}
    </li>
  );
}

/** Says which skills the agent's current version uses that have since been archived: it keeps
 * running with them, but a new version can't include them. */
function ArchivedSkillsNotice({ skills }: { skills: AgentSkill[] }) {
  const archived = skills.filter((s) => s.archived).map((s) => s.name);
  if (!archived.length) return null;
  const one = archived.length === 1;
  return (
    <p role="note" className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm dark:border-amber-700 dark:bg-amber-950">
      {one ? "The skill" : "The skills"} {archived.join(", ")} {one ? "is" : "are"} archived. This
      agent keeps running with {one ? "it" : "them"}, but a change that makes a new version can't
      include {one ? "it" : "them"}: remove {one ? "it" : "them"} first.
    </p>
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
  const listHref = useListHref();
  const { reloadList } = useOutletContext<ListContext>();
  const agent = useLoad(useCallback(() => api.getAgent(id), [id]));
  const versions = useLoad(useCallback(() => api.listAgentVersions(id), [id]));
  const runs = useLoad(useCallback(() => api.listRuns(id), [id]));
  const catalog = useLoad(api.listTools);
  const [actionError, setActionError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [archiving, setArchiving] = useState(false);

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

  const onArchived = async () => {
    setArchiving(false);
    await Promise.all([agent.reload(), reloadList()]);
  };

  const onUnarchive = () =>
    act(async () => {
      await api.unarchiveAgent(id);
      await Promise.all([agent.reload(), reloadList()]);
    });

  const onNewRun = () =>
    act(async () => {
      const run = await api.createRun(id);
      reloadList();
      navigate(listHref(`/agents/${id}/runs/${run.id}`));
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
          {a.archived_at && <ArchivedBadge />}
        </span>
      }
      subtitle={
        <>
          {a.description || `Updated ${formatDate(a.updated_at)}`}
          <ProvenanceLine of={a} className="mt-0.5" />
        </>
      }
      actions={
        <>
          {a.archived_at ? (
            <Button size="sm" onClick={onUnarchive} disabled={busy}>
              <ArchiveRestore />
              Unarchive
            </Button>
          ) : (
            <Button size="sm" onClick={onNewRun} disabled={busy}>
              <Play />
              New run
            </Button>
          )}
          <Button asChild variant="outline" size="sm">
            <Link to={listHref(`/agents/${id}/edit`)}>
              <Pencil />
              Edit
            </Link>
          </Button>
          {!a.archived_at && (
            <Button variant="outline" size="sm" onClick={() => setArchiving(true)}>
              <Archive />
              Archive
            </Button>
          )}
        </>
      }
    >
      <ErrorBanner message={actionError} onDismiss={() => setActionError(undefined)} />
      {a.archived_at && (
        <p role="status" className="bg-muted/50 rounded-md border px-3 py-2 text-sm">
          This agent is archived{a.archived_by ? ` by ${personName(a.archived_by)}` : ""} on{" "}
          {formatDate(a.archived_at)}. It starts nothing new until it's unarchived; its schedules
          stay paused until someone resumes them.
        </p>
      )}
      <ArchivedSkillsNotice skills={current.skills} />
      {archiving && (
        <AgentArchiveDialog
          item={a}
          onClose={() => setArchiving(false)}
          onArchived={onArchived}
        />
      )}

      <Tabs
        label="Agent details"
        tabs={[
          {
            id: "context",
            label: "Context",
            content: (
              <>
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
              </>
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
                    No runs yet. Start one to chat with this agent.
                  </p>
                )}
                {!!runs.data?.length && (
                  <ul className="divide-y rounded-md border">
                    {runs.data.map((run) => (
                      <li key={run.id} className="group flex items-center gap-2 pr-2">
                        <Link
                          to={listHref(`/agents/${id}/runs/${run.id}`)}
                          className="hover:bg-accent flex min-w-0 flex-1 items-center gap-3 px-3 py-2"
                        >
                          {run.trigger_id ? (
                            <CalendarClock
                              className="text-muted-foreground size-4 shrink-0"
                              aria-label="Started by a schedule"
                            />
                          ) : run.chat_id !== null ? (
                            <MessagesSquare
                              className="text-muted-foreground size-4 shrink-0"
                              aria-label="Started from a chat"
                            />
                          ) : (
                            <MessageSquare className="text-muted-foreground size-4 shrink-0" />
                          )}
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm">
                              {run.title ?? (
                                <span className="text-muted-foreground">Untitled run</span>
                              )}
                            </span>
                            <span className="text-muted-foreground block truncate text-xs">
                              {formatDate(run.updated_at)} ·{" "}
                              <RunCountersLine run={run} compact />
                              <StartedBy run={run} before=" · " className="align-bottom" />
                              {run.chat_id !== null && (
                                <span data-testid="started-from-chat">
                                  {" "}
                                  · Started from the chat {run.chat_name}
                                </span>
                              )}
                            </span>
                          </span>
                          <AgentRunStatusBadge run={run} />
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
                href={(v) => listHref(`/agents/${id}/versions/${v}`)}
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
            ),
          },
          {
            id: "schedule",
            label: "Schedule",
            content: <TargetSchedules kind="agent" id={id} />,
          },
        ]}
      />
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
  const listHref = useListHref();
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
  const savedBy = useSavedBy();

  if (version.error) return <Placeholder>{version.error}</Placeholder>;
  if (!version.data || !agent.data) return null;
  const v = version.data;
  const isCurrent = agent.data.current_version.version === v.version;

  async function onRestore() {
    try {
      await api.restoreAgentVersion(id, number);
      await reloadList();
      // Back to where the version was opened from, which now lists the restored copy as current.
      navigate(listHref(`/agents/${id}/versions`));
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <DetailPage
      title={
        <span className="flex items-center gap-2">
          <Link to={listHref(`/agents/${id}/versions`)} className="hover:underline">
            {agent.data.name}
          </Link>
          <VersionBadge version={v.version} />
          {isCurrent && <Badge variant="secondary">current</Badge>}
        </span>
      }
      subtitle={`${v.note ?? "No note"} · ${formatDate(v.created_at)}${savedBy(v.created_by)}`}
      actions={!isCurrent && <RestoreButton onRestore={onRestore} />}
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
          error={versions.error}
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
  const listHref = useListHref();
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
  // Skills the current version uses that have since been archived: not in the list of skills to
  // add, but still attached until removed.
  const archivedPins = [...pinned.values()].filter((p) => p.archived);
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
      navigate(listHref(`/agents/${saved.id}`));
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
          hint="The agent's core instructions, in markdown. Attached skills are appended after it."
        >
          <MarkdownEditor
            id="agent-prompt"
            rows={10}
            className="font-mono text-sm"
            previewClassName="max-h-[32rem]"
            value={form.base_prompt}
            onChange={(e) => set({ base_prompt: e.target.value })}
            placeholder="You are a support engineer. Classify each ticket…"
          />
        </Field>
        <fieldset className="flex flex-col gap-1.5">
          <legend className="mb-1.5 text-sm font-medium">Skills</legend>
          {skills?.length === 0 && !archivedPins.length && (
            <p className="text-muted-foreground text-sm">
              You have no skills yet.{" "}
              <Link to="/skills/new" className="underline">
                Create one
              </Link>
              .
            </p>
          )}
          {(!!skills?.length || !!archivedPins.length) && (
            <ul className="divide-y rounded-md border">
              {skills?.map((skill) => {
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
              {archivedPins.map((pin) => (
                <li key={pin.skill_id}>
                  <label className="hover:bg-accent flex cursor-pointer items-start gap-3 px-3 py-2">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={form.skill_ids.includes(pin.skill_id)}
                      onChange={(e) => toggleSkill(pin.skill_id, e.target.checked)}
                    />
                    <span className="min-w-0 flex-1 text-sm font-medium">{pin.name}</span>
                    <VersionBadge version={pin.version} />
                    <Badge variant="secondary">archived, remove to make a new version</Badge>
                  </label>
                </li>
              ))}
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
                  {attached && Object.keys(tool.config_schema.properties ?? {}).length > 0 && (
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
