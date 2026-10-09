import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams } from "react-router";
import { Pencil, Trash2 } from "lucide-react";
import Markdown from "react-markdown";

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
import { api, describeError, type SkillInput } from "@/lib/api";

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
          <div className="truncate font-medium">{skill.name}</div>
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

export function SkillDetail() {
  const id = Number(useParams().skillId);
  const navigate = useNavigate();
  const { reloadList } = useOutletContext<ListContext>();
  const { data: skill, error } = useLoad(useCallback(() => api.getSkill(id), [id]));
  const [deleteError, setDeleteError] = useState<string>();

  if (error) return <Placeholder>{error}</Placeholder>;
  if (!skill) return null;

  async function onDelete() {
    const usage = skill!.agents.length
      ? ` It will be removed from ${skill!.agents.length} agent(s).`
      : "";
    if (!window.confirm(`Delete the skill “${skill!.name}”?${usage}`)) return;
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
      title={skill.name}
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
      <Section title="When to use">
        <p className="text-sm whitespace-pre-wrap">
          {skill.description || <span className="text-muted-foreground">No description.</span>}
        </p>
      </Section>
      <Section title="Instructions">
        {skill.content ? (
          <div className="prose-sm rounded-md border p-4 leading-relaxed [&_code]:text-sm [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-2 [&_pre]:overflow-x-auto [&_ul]:list-disc [&_ul]:pl-5">
            <Markdown>{skill.content}</Markdown>
          </div>
        ) : (
          <p className="text-muted-foreground text-sm">No instructions.</p>
        )}
      </Section>
      <Section title="Used by">
        {skill.agents.length === 0 ? (
          <p className="text-muted-foreground text-sm">No agents use this skill yet.</p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {skill.agents.map((agent) => (
              <Badge key={agent.id} variant="outline" asChild>
                <Link to={`/agents/${agent.id}`}>{agent.name}</Link>
              </Badge>
            ))}
          </div>
        )}
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
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setForm(undefined);
    if (id === undefined) setForm(emptySkill);
    else
      api.getSkill(id).then(
        ({ name, description, content }) => setForm({ name, description, content }),
        (err) => setError(describeError(err)),
      );
  }, [id]);

  if (!form) return error ? <Placeholder>{error}</Placeholder> : null;
  const set = (patch: Partial<SkillInput>) => setForm({ ...form, ...patch });

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    try {
      const saved = id === undefined ? await api.createSkill(form!) : await api.updateSkill(id, form!);
      await reloadList();
      navigate(`/skills/${saved.id}`);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DetailPage title={id === undefined ? "New skill" : "Edit skill"}>
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
          <Textarea
            id="skill-content"
            rows={14}
            className="font-mono text-sm"
            value={form.content}
            onChange={(e) => set({ content: e.target.value })}
            placeholder={"1. Group changes by area.\n2. Lead with user-visible changes.\n…"}
          />
        </Field>
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
