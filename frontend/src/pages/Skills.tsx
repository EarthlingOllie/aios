import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams, useSearchParams } from "react-router";
import { Archive, ArchiveRestore, Pencil } from "lucide-react";

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
import { SkillArchiveDialog } from "@/components/ArchiveDialog";
import { MarkdownView } from "@/components/Markdown";
import { MarkdownEditor } from "@/components/MarkdownEditor";
import { Tabs } from "@/components/tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ProvenanceLine, useSavedBy } from "@/components/provenance";
import {
  CompareVersions,
  RestoreButton,
  VersionBadge,
  VersionHistory,
} from "@/components/versions";
import {
  api,
  describeError,
  type SkillInput,
  type SkillSummary,
  type SkillVersion,
} from "@/lib/api";
import {
  nameAndDescription,
  NAME_SORT,
  UPDATED_SORT,
  type ListFilterGroup,
  type ListSort,
} from "@/lib/list-filter";
import { plural } from "@/lib/utils";
import { ArchivedBadge } from "@/pages/Agents";

const SKILL_FILTERS: ListFilterGroup<SkillSummary>[] = [
  {
    param: "filter",
    label: "Show",
    all: "All",
    // Used: an agent's current version uses some version of it.
    options: [
      { key: "used", label: "Used", test: (skill) => skill.agent_count > 0 },
      { key: "unused", label: "Unused", test: (skill) => skill.agent_count === 0 },
      // The list is loaded from the archived skills for this one (`SkillsSection`); the others
      // narrow the default list, which leaves archived skills out.
      { key: "archived", label: "Archived", test: (skill) => skill.archived_at !== null },
    ],
  },
];

const SKILL_SORTS: ListSort<SkillSummary>[] = [NAME_SORT, UPDATED_SORT];

export function SkillsSection() {
  const [params] = useSearchParams();
  const archived = params.get("filter") === "archived";
  const { data, error, reload } = useLoad(
    useCallback(() => api.listSkills(archived), [archived]),
  );
  return (
    <MasterDetail
      title="Skills"
      basePath="/skills"
      items={data}
      error={error}
      empty="No skills yet. Skills are reusable instructions you can give to agents."
      reloadList={reload}
      search={nameAndDescription}
      filters={SKILL_FILTERS}
      sorts={SKILL_SORTS}
      renderItem={(skill) => {
        const about = [skill.agent_count > 0 && `Used by ${plural(skill.agent_count, "agent")}`]
          .concat(skill.description)
          .filter(Boolean)
          .join(" · ");
        return (
          <>
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate font-medium">{skill.name}</span>
              {skill.archived_at && <ArchivedBadge />}
              <VersionBadge version={skill.version} />
            </div>
            {about && <div className="text-muted-foreground truncate text-xs">{about}</div>}
            <ProvenanceLine of={skill} />
          </>
        );
      }}
    />
  );
}

export function SkillsIndex() {
  return <Placeholder>Select a skill, or create a new one.</Placeholder>;
}

function SkillContent({ version }: { version: SkillVersion }) {
  return (
    <>
      <Section title="When to use">
        <p className="text-sm whitespace-pre-wrap">
          {version.description || <span className="text-muted-foreground">No description.</span>}
        </p>
      </Section>
      <Section title="Instructions">
        {version.content ? (
          <MarkdownView className="rounded-md border p-4">{version.content}</MarkdownView>
        ) : (
          <p className="text-muted-foreground text-sm">No instructions.</p>
        )}
      </Section>
    </>
  );
}

export function SkillDetail() {
  const id = Number(useParams().skillId);
  const listHref = useListHref();
  const { reloadList } = useOutletContext<ListContext>();
  const { data: skill, error, reload } = useLoad(useCallback(() => api.getSkill(id), [id]));
  const versions = useLoad(useCallback(() => api.listSkillVersions(id), [id]));
  const [actionError, setActionError] = useState<string>();
  const [archiving, setArchiving] = useState(false);
  const [busy, setBusy] = useState(false);

  if (error) return <Placeholder>{error}</Placeholder>;
  if (!skill) return null;
  const current = skill.current_version;

  const onArchived = async () => {
    setArchiving(false);
    await Promise.all([reload(), reloadList()]);
  };

  async function onUnarchive() {
    setBusy(true);
    setActionError(undefined);
    try {
      await api.unarchiveSkill(id);
      await Promise.all([reload(), reloadList()]);
    } catch (err) {
      setActionError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <DetailPage
      title={
        <span className="flex items-center gap-2">
          {skill.name} <VersionBadge version={current.version} />
          {skill.archived_at && <ArchivedBadge />}
        </span>
      }
      subtitle={
        <>
          Updated {formatDate(skill.updated_at)}
          <ProvenanceLine of={skill} className="mt-0.5" />
        </>
      }
      actions={
        <>
          <Button asChild variant="outline" size="sm">
            <Link to={listHref(`/skills/${id}/edit`)}>
              <Pencil />
              Edit
            </Link>
          </Button>
          {skill.archived_at ? (
            <Button variant="outline" size="sm" onClick={onUnarchive} disabled={busy}>
              <ArchiveRestore />
              Unarchive
            </Button>
          ) : (
            <Button variant="outline" size="sm" onClick={() => setArchiving(true)}>
              <Archive />
              Archive
            </Button>
          )}
        </>
      }
    >
      <ErrorBanner message={actionError} onDismiss={() => setActionError(undefined)} />
      {skill.archived_at && (
        <p role="status" className="bg-muted/50 rounded-md border px-3 py-2 text-sm">
          This skill is archived{skill.archived_by ? ` by ${skill.archived_by.name}` : ""} on{" "}
          {formatDate(skill.archived_at)}. It can't be added to an agent until it's unarchived;
          agents that use it keep running with it.
        </p>
      )}
      {archiving && (
        <SkillArchiveDialog
          item={skill}
          onClose={() => setArchiving(false)}
          onArchived={onArchived}
        />
      )}
      <Tabs
        label="Skill details"
        tabs={[
          { id: "content", label: "Content", content: <SkillContent version={current} /> },
          {
            id: "used-by",
            label: "Used by",
            count: skill.agents.length,
            content:
              skill.agents.length === 0 ? (
                <p className="text-muted-foreground text-sm">No agents use this skill yet.</p>
              ) : (
                <ul className="divide-y rounded-md border">
                  {skill.agents.map((use) => (
                    <li key={use.agent_id}>
                      <Link
                        to={`/agents/${use.agent_id}`}
                        className="hover:bg-accent flex items-center gap-2 px-3 py-2 text-sm"
                      >
                        <span className="flex-1">{use.agent_name}</span>
                        <span className="text-muted-foreground text-xs">uses</span>
                        <VersionBadge version={use.skill_version} />
                        {use.skill_version < current.version && (
                          <Badge variant="secondary">update available</Badge>
                        )}
                      </Link>
                    </li>
                  ))}
                </ul>
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
                href={(v) => listHref(`/skills/${id}/versions/${v}`)}
              />
            ),
          },
        ]}
      />
    </DetailPage>
  );
}

export function SkillVersionPage() {
  const id = Number(useParams().skillId);
  const number = Number(useParams().version);
  // Keyed so per-version state (e.g. the chosen comparison base) resets when
  // navigating to another version.
  return <SkillVersionView key={`${id}/${number}`} id={id} number={number} />;
}

function SkillVersionView({ id, number }: { id: number; number: number }) {
  const navigate = useNavigate();
  const listHref = useListHref();
  const { reloadList } = useOutletContext<ListContext>();
  const skill = useLoad(useCallback(() => api.getSkill(id), [id]));
  const version = useLoad(useCallback(() => api.getSkillVersion(id, number), [id, number]));
  const versions = useLoad(useCallback(() => api.listSkillVersions(id), [id]));
  const older = (versions.data ?? []).map((v) => v.version).filter((v) => v < number);
  const [base, setBase] = useState<number>();
  const baseVersion = base ?? older[0];
  const before = useLoad(
    useCallback(
      () =>
        baseVersion === undefined
          ? Promise.resolve(undefined)
          : api.getSkillVersion(id, baseVersion),
      [id, baseVersion],
    ),
  );
  const [error, setError] = useState<string>();
  const savedBy = useSavedBy();

  if (version.error) return <Placeholder>{version.error}</Placeholder>;
  if (!version.data || !skill.data) return null;
  const v = version.data;
  const isCurrent = skill.data.current_version.version === v.version;

  async function onRestore() {
    try {
      await api.restoreSkillVersion(id, number);
      await reloadList();
      navigate(listHref(`/skills/${id}/versions`));
    } catch (err) {
      setError(describeError(err));
    }
  }

  const asText = (s: SkillVersion) => `${s.description}\n\n${s.content}`;
  return (
    <DetailPage
      title={
        <span className="flex items-center gap-2">
          <Link to={listHref(`/skills/${id}/versions`)} className="hover:underline">
            {skill.data.name}
          </Link>
          <VersionBadge version={v.version} />
          {isCurrent && <Badge variant="secondary">current</Badge>}
        </span>
      }
      subtitle={`${v.note ?? "No note"} · ${formatDate(v.created_at)}${savedBy(v.created_by)}`}
      actions={!isCurrent && <RestoreButton onRestore={onRestore} />}
    >
      <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
      <SkillContent version={v} />
      <Section title="Changes">
        <CompareVersions
          versions={older}
          error={versions.error}
          base={baseVersion}
          onBaseChange={setBase}
          before={before.data ? asText(before.data) : undefined}
          after={asText(v)}
        />
      </Section>
    </DetailPage>
  );
}

const emptySkill: SkillInput = { name: "", description: "", content: "" };

export function SkillForm() {
  const params = useParams();
  const id = params.skillId ? Number(params.skillId) : undefined;
  const navigate = useNavigate();
  const listHref = useListHref();
  const { reloadList } = useOutletContext<ListContext>();
  const [form, setForm] = useState<SkillInput>();
  const [version, setVersion] = useState<number>();
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setForm(undefined);
    if (id === undefined) setForm(emptySkill);
    else
      api.getSkill(id).then(
        ({ name, current_version: v }) => {
          setForm({ name, description: v.description, content: v.content, note: "" });
          setVersion(v.version);
        },
        (err) => setError(describeError(err)),
      );
  }, [id]);

  if (!form) return error ? <Placeholder>{error}</Placeholder> : null;
  const set = (patch: Partial<SkillInput>) => setForm({ ...form, ...patch });

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    const body = { ...form!, note: form!.note?.trim() || undefined };
    try {
      const saved = id === undefined ? await api.createSkill(body) : await api.updateSkill(id, body);
      await reloadList();
      navigate(listHref(`/skills/${saved.id}`));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DetailPage
      title={id === undefined ? "New skill" : "Edit skill"}
      subtitle={
        version !== undefined &&
        `Editing v${version}. Changing the description or instructions creates v${version + 1}; agents keep using the version they're pinned to until you update them.`
      }
    >
      <form className="flex flex-col gap-5" onSubmit={onSubmit}>
        <ErrorBanner message={error} onDismiss={() => setError(undefined)} />
        <Field label="Name" htmlFor="skill-name">
          <Input
            id="skill-name"
            required
            maxLength={255}
            value={form.name}
            onChange={(e) => set({ name: e.target.value })}
            placeholder="e.g. Release notes"
          />
        </Field>
        <Field
          label="Description"
          htmlFor="skill-description"
          hint="When should an agent use this skill? Agents see this alongside the instructions."
        >
          <Textarea
            id="skill-description"
            rows={2}
            value={form.description}
            onChange={(e) => set({ description: e.target.value })}
            placeholder="Use when asked to summarise a set of changes for users."
          />
        </Field>
        <Field label="Instructions" htmlFor="skill-content" hint="Markdown is supported.">
          <MarkdownEditor
            id="skill-content"
            rows={14}
            className="font-mono text-sm"
            previewClassName="max-h-[32rem]"
            value={form.content}
            onChange={(e) => set({ content: e.target.value })}
            placeholder={"1. Group changes by area.\n2. Lead with user-visible changes.\n…"}
          />
        </Field>
        {id !== undefined && (
          <Field label="Change note" htmlFor="skill-note" hint="Optional. Shown in the version history.">
            <Input
              id="skill-note"
              value={form.note ?? ""}
              onChange={(e) => set({ note: e.target.value })}
              placeholder="What changed and why"
            />
          </Field>
        )}
        <div className="flex gap-2">
          <Button type="submit" disabled={saving || !form.name.trim()}>
            {id === undefined ? "Create skill" : "Save changes"}
          </Button>
          <Button type="button" variant="ghost" onClick={() => navigate(-1)}>
            Cancel
          </Button>
        </div>
      </form>
    </DetailPage>
  );
}
