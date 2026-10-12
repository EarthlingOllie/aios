import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams } from "react-router";
import { History, Pencil, Trash2 } from "lucide-react";

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
import { MarkdownView } from "@/components/Markdown";
import { MarkdownEditor } from "@/components/MarkdownEditor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { CompareVersions, VersionBadge, VersionHistory } from "@/components/versions";
import { api, describeError, type SkillInput, type SkillVersion } from "@/lib/api";

export function SkillsSection() {
  const { data, error, reload } = useLoad(api.listSkills);
  return (
    <MasterDetail
      title="Skills"
      basePath="/skills"
      items={data}
      error={error}
      empty="No skills yet. Skills are reusable instructions you can give to agents."
      reloadList={reload}
      renderItem={(skill) => (
        <>
          <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate font-medium">{skill.name}</span>
            <VersionBadge version={skill.version} />
          </div>
          {skill.description && (
            <div className="text-muted-foreground truncate text-xs">{skill.description}</div>
          )}
        </>
      )}
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
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const { data: skill, error } = useLoad(useCallback(() => api.getSkill(id), [id]));
  const versions = useLoad(useCallback(() => api.listSkillVersions(id), [id]));
  const [deleteError, setDeleteError] = useState<string>();

  if (error) return <Placeholder>{error}</Placeholder>;
  if (!skill) return null;
  const current = skill.current_version;

  async function onDelete() {
    if (!window.confirm(`Delete the skill “${skill!.name}”?`)) return;
    try {
      await api.deleteSkill(id);
      await reloadList();
      navigate("/skills");
    } catch (err) {
      setDeleteError(describeError(err));
    }
  }

  return (
    <DetailPage
      title={
        <span className="flex items-center gap-2">
          {skill.name} <VersionBadge version={current.version} />
        </span>
      }
      subtitle={`Updated ${formatDate(skill.updated_at)}`}
      actions={
        <>
          <Button asChild variant="outline" size="sm">
            <Link to="edit">
              <Pencil />
              Edit
            </Link>
          </Button>
          <Button variant="outline" size="sm" onClick={onDelete}>
            <Trash2 />
            Delete
          </Button>
        </>
      }
    >
      <ErrorBanner message={deleteError} onDismiss={() => setDeleteError(undefined)} />
      <SkillContent version={current} />
      <Section title="Used by">
        {skill.agents.length === 0 ? (
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
        )}
      </Section>
      <Section title="Version history">
        <VersionHistory
          versions={versions.data}
          current={current.version}
          href={(v) => `/skills/${id}/versions/${v}`}
        />
      </Section>
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

  if (version.error) return <Placeholder>{version.error}</Placeholder>;
  if (!version.data || !skill.data) return null;
  const v = version.data;
  const isCurrent = skill.data.current_version.version === v.version;

  async function onRestore() {
    try {
      await api.restoreSkillVersion(id, number);
      await reloadList();
      navigate(`/skills/${id}`);
    } catch (err) {
      setError(describeError(err));
    }
  }

  const asText = (s: SkillVersion) => `${s.description}\n\n${s.content}`;
  return (
    <DetailPage
      title={
        <span className="flex items-center gap-2">
          <Link to={`/skills/${id}`} className="hover:underline">
            {skill.data.name}
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
      <SkillContent version={v} />
      <Section title="Changes">
        <CompareVersions
          versions={older}
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
      navigate(`/skills/${saved.id}`);
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
