import { useState, type FormEvent, type ReactNode } from "react";

import { ErrorBanner, Section } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { formErrors } from "@/lib/auth";
import { useCurrentUser, useSetCurrentUser } from "@/lib/current-user";
import { Field } from "@/pages/Auth";

/**
 * The Settings page's Profile section: the signed-in user's own details. Each detail has its own
 * form, saved on its own, so a mistake in one doesn't block another.
 */
export function ProfileSection() {
  return (
    <Section title="Profile" label="Profile">
      <div className="grid gap-4 md:grid-cols-2">
        <NameForm />
      </div>
    </Section>
  );
}

/** A user's first and last name. No password needed. */
function NameForm() {
  const user = useCurrentUser();
  const setUser = useSetCurrentUser();
  const [names, setNames] = useState({
    first_name: user?.first_name ?? "",
    last_name: user?.last_name ?? "",
  });
  const form = useProfileForm();
  const changed = names.first_name !== user?.first_name || names.last_name !== user?.last_name;

  return (
    <ProfileForm
      title="Name"
      form={form}
      onSubmit={(e) =>
        form.submit(e, async () => {
          const saved = await api.updateMe(names);
          // As the API stored them (trimmed), here and everywhere else they're shown.
          setNames({ first_name: saved.first_name, last_name: saved.last_name });
          setUser(saved);
          return "Name saved.";
        })
      }
    >
      <Field label="First name" error={form.fields.first_name}>
        {(props) => (
          <Input
            {...props}
            autoComplete="given-name"
            maxLength={100}
            value={names.first_name}
            onChange={(e) => setNames((n) => ({ ...n, first_name: e.target.value }))}
          />
        )}
      </Field>
      <Field label="Last name" error={form.fields.last_name}>
        {(props) => (
          <Input
            {...props}
            autoComplete="family-name"
            maxLength={100}
            value={names.last_name}
            onChange={(e) => setNames((n) => ({ ...n, last_name: e.target.value }))}
          />
        )}
      </Field>
      <Button type="submit" size="sm" disabled={form.busy || !changed} aria-label="Save name">
        Save
      </Button>
    </ProfileForm>
  );
}

type ProfileFormState = ReturnType<typeof useProfileForm>;

/**
 * A profile form's request: `submit` sends one at a time, then shows what `send` says it did, or
 * what went wrong: a refusal above the form, each invalid field's message (422) next to it.
 */
function useProfileForm() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [fields, setFields] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string>();

  async function submit(e: FormEvent, send: () => Promise<string>) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(undefined);
    setFields({});
    setNotice(undefined);
    try {
      setNotice(await send());
    } catch (err) {
      const errors = formErrors(err);
      setError(errors.form);
      setFields(errors.fields);
    } finally {
      setBusy(false);
    }
  }

  return { busy, error, fields, notice, submit };
}

function ProfileForm({
  title,
  form,
  onSubmit,
  children,
}: {
  title: string;
  form: ProfileFormState;
  onSubmit: (e: FormEvent) => void;
  children: ReactNode;
}) {
  return (
    <form
      className="flex flex-col gap-3 rounded-md border p-3"
      aria-label={title}
      onSubmit={onSubmit}
      noValidate
    >
      <h4 className="text-sm font-medium">{title}</h4>
      <ErrorBanner message={form.error} />
      {children}
      <p role="status" className="text-muted-foreground text-sm empty:hidden">
        {form.notice}
      </p>
    </form>
  );
}
