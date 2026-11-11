import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, type Me } from "@/lib/api";
import { formErrors } from "@/lib/auth";
import { landingPath, openInOrganisation, recentOrganisations } from "@/lib/organisation-path";
import { Field } from "@/pages/Auth";

/**
 * The welcome page (`/welcome`), outside any organisation: where a person in none lands (signing
 * up makes nothing), and where the switcher's "Create an organisation" goes. It creates an
 * organisation from a name, with the person as its creator, and opens it; and explains that an
 * invitation link is the way into a colleague's.
 */
export function WelcomePage({ me, onSignOut }: { me: Me; onSignOut: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [fieldError, setFieldError] = useState<string>();
  const first = me.organisations.length === 0;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(undefined);
    setFieldError(undefined);
    try {
      const made = await api.createOrganisation(name.trim());
      // A new page load: the organisation is the router's basename.
      openInOrganisation(made.slug, "/");
    } catch (err) {
      const errors = formErrors(err);
      setError(errors.form);
      setFieldError(errors.fields.name);
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-svh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-xl" role="heading" aria-level={1}>
            {first ? "Welcome to aios" : "Create an organisation"}
          </CardTitle>
          <CardDescription>
            An organisation holds your agents, chats, workflows and data, shared with everyone in it.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {error && (
            <p role="alert" className="text-destructive rounded-md border border-current/30 px-3 py-2 text-sm">
              {error}
            </p>
          )}
          <form className="flex flex-col gap-4" onSubmit={submit} noValidate>
            <Field label="Organisation name" error={fieldError}>
              {(props) => (
                <Input
                  {...props}
                  required
                  maxLength={255}
                  autoFocus
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              )}
            </Field>
            <Button type="submit" disabled={busy || !name.trim()}>
              Create organisation
            </Button>
          </form>
          <p className="text-muted-foreground text-sm">
            Joining your colleagues instead? Open the invitation link one of them sends you: it adds
            you to their organisation.
          </p>
          <div className="text-muted-foreground flex justify-between gap-2 text-sm">
            {me.organisations.length > 0 ? (
              // The one used most recently; a new page load, as the organisation is the router's
              // basename.
              <a href={landingPath(me.organisations, recentOrganisations())} className="underline underline-offset-4">
                Back to your organisations
              </a>
            ) : (
              <span />
            )}
            <button type="button" className="underline underline-offset-4" onClick={onSignOut}>
              Sign out
            </button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
