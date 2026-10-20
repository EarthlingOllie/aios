import { useState, type FormEvent, type ReactNode } from "react";

import { ErrorBanner, Section } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { formErrors } from "@/lib/auth";
import { useCurrentUser, useSetCurrentUser } from "@/lib/current-user";
import { Field, useConfirmation } from "@/pages/Auth";

/**
 * The Settings page's Profile section: the signed-in user's own details. Each detail has its own
 * form, saved on its own, so a mistake in one doesn't block another.
 */
export function ProfileSection() {
  return (
    <Section title="Profile" label="Profile">
      <div className="grid gap-4 md:grid-cols-2">
        <NameForm />
        <EmailForm />
        <PasswordForm />
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

/** The email a user signs in with. Changing it takes their current password. */
function EmailForm() {
  const user = useCurrentUser();
  const setUser = useSetCurrentUser();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const form = useProfileForm();

  return (
    <ProfileForm
      title="Email"
      form={form}
      onSubmit={(e) =>
        form.submit(e, async () => {
          const saved = await api.changeEmail({ email, current_password: password });
          setUser(saved);
          setEmail("");
          setPassword("");
          return "Email changed.";
        })
      }
    >
      <p className="text-muted-foreground text-sm">Currently {user?.email}.</p>
      <Field label="New email" error={form.fields.email}>
        {(props) => (
          <Input
            {...props}
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        )}
      </Field>
      <CurrentPassword form={form} value={password} onChange={setPassword} />
      <Button type="submit" size="sm" disabled={form.busy || !email.trim() || !password}>
        Change email
      </Button>
    </ProfileForm>
  );
}

/** A user's password: the current one, then the new one twice. Signs out their other sessions. */
function PasswordForm() {
  const [passwords, setPasswords] = useState({
    current_password: "",
    new_password: "",
    new_password_confirm: "",
  });
  const confirmation = useConfirmation(passwords.new_password, passwords.new_password_confirm);
  const form = useProfileForm();
  const set = (key: keyof typeof passwords) => (value: string) =>
    setPasswords((p) => ({ ...p, [key]: value }));

  return (
    <ProfileForm
      title="Password"
      form={form}
      onSubmit={(e) => {
        e.preventDefault();
        if (!confirmation.check()) return;
        form.submit(e, async () => {
          await api.changePassword(passwords);
          setPasswords({ current_password: "", new_password: "", new_password_confirm: "" });
          return "Password changed. You've been signed out everywhere else.";
        });
      }}
    >
      <CurrentPassword form={form} value={passwords.current_password} onChange={set("current_password")} />
      <Field label="New password" hint="At least 12 characters." error={form.fields.new_password}>
        {(props) => (
          <Input
            {...props}
            type="password"
            autoComplete="new-password"
            value={passwords.new_password}
            onChange={(e) => set("new_password")(e.target.value)}
          />
        )}
      </Field>
      <Field
        label="Confirm new password"
        error={confirmation.error ?? form.fields.new_password_confirm}
      >
        {(props) => (
          <Input
            {...props}
            type="password"
            autoComplete="new-password"
            value={passwords.new_password_confirm}
            onChange={(e) => set("new_password_confirm")(e.target.value)}
            onBlur={confirmation.onBlur}
          />
        )}
      </Field>
      <Button
        type="submit"
        size="sm"
        disabled={form.busy || !passwords.current_password || !passwords.new_password}
      >
        Change password
      </Button>
    </ProfileForm>
  );
}

/** The current password a form needs; a wrong one is refused above the form (401). */
function CurrentPassword({
  form,
  value,
  onChange,
}: {
  form: ProfileFormState;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <Field label="Current password" error={form.fields.current_password}>
      {(props) => (
        <Input
          {...props}
          type="password"
          autoComplete="current-password"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </Field>
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
