import { useId, useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, type User } from "@/lib/api";
import { formErrors } from "@/lib/auth";

type Props = {
  /** Called with the user once they're signed in; the app then takes them on (see `App`). */
  onSignedIn: (user: User) => void;
};

const MISMATCH = "Passwords don't match";

/** Sign in with an email and password: `/login`. */
export function LoginPage({ onSignedIn }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const form = useAuthForm(() => api.login({ email, password }), onSignedIn);

  return (
    <AuthCard
      title="Sign in"
      error={form.error}
      footer={
        <>
          New here? <SwitchLink to="/register">Create an account</SwitchLink>
        </>
      }
    >
      <form className="flex flex-col gap-4" onSubmit={form.submit} noValidate>
        <Field label="Email" error={form.fields.email}>
          {(props) => (
            <Input
              {...props}
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          )}
        </Field>
        <Field label="Password" error={form.fields.password}>
          {(props) => (
            <Input
              {...props}
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
        </Field>
        <Button type="submit" disabled={form.busy}>
          Sign in
        </Button>
      </form>
    </AuthCard>
  );
}

/** Create an account: `/register`. Signs the new user in. */
export function RegisterPage({ onSignedIn }: Props) {
  const [details, setDetails] = useState({
    first_name: "",
    last_name: "",
    email: "",
    password: "",
    password_confirm: "",
  });
  // Shown once the confirmation is left (or the form submitted) while it differs.
  const [mismatch, setMismatch] = useState(false);
  const differ = details.password_confirm !== details.password;
  const form = useAuthForm(() => api.register(details), onSignedIn, () => {
    setMismatch(differ);
    return !differ;
  });
  const set = (key: keyof typeof details) => (e: ChangeEvent<HTMLInputElement>) => {
    setDetails((d) => ({ ...d, [key]: e.target.value }));
  };

  return (
    <AuthCard
      title="Create an account"
      error={form.error}
      footer={
        <>
          Already have an account? <SwitchLink to="/login">Sign in</SwitchLink>
        </>
      }
    >
      <form className="flex flex-col gap-4" onSubmit={form.submit} noValidate>
        <div className="grid grid-cols-2 gap-3">
          <Field label="First name" error={form.fields.first_name}>
            {(props) => (
              <Input
                {...props}
                autoComplete="given-name"
                required
                maxLength={100}
                value={details.first_name}
                onChange={set("first_name")}
              />
            )}
          </Field>
          <Field label="Last name" error={form.fields.last_name}>
            {(props) => (
              <Input
                {...props}
                autoComplete="family-name"
                required
                maxLength={100}
                value={details.last_name}
                onChange={set("last_name")}
              />
            )}
          </Field>
        </div>
        <Field label="Email" error={form.fields.email}>
          {(props) => (
            <Input
              {...props}
              type="email"
              autoComplete="email"
              required
              value={details.email}
              onChange={set("email")}
            />
          )}
        </Field>
        <Field
          label="Password"
          hint="At least 12 characters."
          error={form.fields.password}
        >
          {(props) => (
            <Input
              {...props}
              type="password"
              autoComplete="new-password"
              required
              value={details.password}
              onChange={set("password")}
            />
          )}
        </Field>
        <Field
          label="Confirm password"
          error={(mismatch && differ ? MISMATCH : undefined) ?? form.fields.password_confirm}
        >
          {(props) => (
            <Input
              {...props}
              type="password"
              autoComplete="new-password"
              required
              value={details.password_confirm}
              onChange={set("password_confirm")}
              onBlur={() => setMismatch(differ)}
            />
          )}
        </Field>
        <Button type="submit" disabled={form.busy}>
          Create account
        </Button>
      </form>
    </AuthCard>
  );
}

/**
 * A sign-in or registration form's request: `send` it on submit (unless `check` says not to),
 * one at a time, then hand the user on, or say what went wrong.
 */
function useAuthForm(send: () => Promise<User>, onSignedIn: (user: User) => void, check?: () => boolean) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [fields, setFields] = useState<Record<string, string>>({});

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || (check && !check())) return;
    setBusy(true);
    setError(undefined);
    setFields({});
    try {
      const user = await send();
      onSignedIn(user);
    } catch (err) {
      const errors = formErrors(err);
      setError(errors.form);
      setFields(errors.fields);
      setBusy(false);
    }
  }

  return { busy, error, fields, submit };
}

function AuthCard({
  title,
  error,
  footer,
  children,
}: {
  title: string;
  error?: string;
  footer: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-svh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-xl">aios</CardTitle>
          <CardDescription>{title}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {error && (
            <p role="alert" className="text-destructive rounded-md border border-current/30 px-3 py-2 text-sm">
              {error}
            </p>
          )}
          {children}
          <p className="text-muted-foreground text-center text-sm">{footer}</p>
        </CardContent>
      </Card>
    </div>
  );
}

/** A labelled input, with an optional hint and its error underneath. Settings' profile forms use it too. */
export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  children: (props: { id: string; "aria-invalid"?: true; "aria-describedby"?: string }) => ReactNode;
}) {
  const id = useId();
  const below = error ?? hint;
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {children({
        id,
        "aria-invalid": error ? true : undefined,
        "aria-describedby": below ? `${id}-below` : undefined,
      })}
      {below && (
        <p id={`${id}-below`} className={error ? "text-destructive text-xs" : "text-muted-foreground text-xs"}>
          {below}
        </p>
      )}
    </div>
  );
}

function SwitchLink({ to, children }: { to: string; children: ReactNode }) {
  // Keep where the user was headed, whichever form they finish on.
  const [params] = useSearchParams();
  const next = params.get("next");
  return (
    <Link to={next ? `${to}?next=${encodeURIComponent(next)}` : to} className="text-foreground underline">
      {children}
    </Link>
  );
}
