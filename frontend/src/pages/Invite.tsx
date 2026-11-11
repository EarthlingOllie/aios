import { useCallback, useState } from "react";
import { Link, useParams } from "react-router";
import { Loader2 } from "lucide-react";

import { useLoad } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError, describeError, type Me } from "@/lib/api";
import { useCurrentUser, useReloadCurrentUser } from "@/lib/current-user";
import { inOrganisation, openInOrganisation } from "@/lib/organisation-path";
import { aRole } from "@/lib/permissions";

/** The invite page in the shell, for a signed-in user. */
export function InviteRoute() {
  const { token } = useParams();
  return <InvitePage key={token} token={token!} me={useCurrentUser()} />;
}

/**
 * An invitation link's page (`/invite/:token`), signed in or out: which organisation it's for,
 * and Accept, or, signed out, Register (which sends the token with the registration, joining the
 * new user) and Sign in, each coming back here. An expired or revoked link says so. Accepting
 * adds the organisation to the ones the user is in, and opens it; a user already in it is told
 * so, with a link to it.
 */
export function InvitePage({ token, me }: { token: string; me: Me | null }) {
  const reloadMe = useReloadCurrentUser();
  const { data: invitation, error } = useLoad(
    useCallback(() => api.getInvitation(token).catch(gone), [token]),
  );
  const [accepting, setAccepting] = useState(false);
  const [refusal, setRefusal] = useState<string>();

  async function onAccept() {
    setAccepting(true);
    setRefusal(undefined);
    try {
      const joined = await api.acceptInvitation(token);
      // Its page, at its own link.
      openInOrganisation(joined.slug, "/organisation");
    } catch (err) {
      // The session may be behind (the link revoked meanwhile, say): read it again.
      reloadMe();
      setRefusal(describeError(err));
      setAccepting(false);
    }
  }

  let title: string;
  let body: React.ReactNode = null;
  if (invitation === undefined) {
    title = error ? "Couldn't open the invitation" : "Invitation";
    body = error ? (
      <p className="text-muted-foreground text-sm">{error}</p>
    ) : (
      <Loader2 className="size-4 animate-spin" />
    );
  } else if (invitation === null) {
    title = "This invitation link no longer works";
    body = (
      <p className="text-muted-foreground text-sm">
        It has expired or been revoked. Ask whoever sent it for a new one.
      </p>
    );
  } else {
    const { organisation } = invitation;
    title = `Join ${organisation.name}`;
    if (!me) {
      // Where registering or signing in comes back to: this page.
      const here = `/invite/${encodeURIComponent(token)}`;
      body = (
        <>
          <p className="text-muted-foreground text-sm">
            You've been invited to join {organisation.name} on aios. Create an account to accept.
          </p>
          <Button asChild className="w-full">
            <Link to={`/register?${new URLSearchParams({ invite: token, next: here })}`}>
              Create an account to accept
            </Link>
          </Button>
          <p className="text-muted-foreground text-center text-sm">
            Already have an account?{" "}
            <Link
              to={`/login?${new URLSearchParams({ next: here })}`}
              className="text-foreground underline underline-offset-4"
            >
              Sign in
            </Link>
          </p>
        </>
      );
    } else {
      const joined = me.organisations.find((o) => o.id === organisation.id);
      body = joined ? (
        <p className="text-muted-foreground text-sm">
          You're already a member of {organisation.name}. See it on its{" "}
          {/* A new page load: the organisation is the router's basename. */}
          <a
            href={inOrganisation(joined.slug, "/organisation")}
            className="text-foreground underline underline-offset-4"
          >
            Organisation
          </a>{" "}
          page.
        </p>
      ) : (
        <>
          <p className="text-muted-foreground text-sm">
            You've been invited to join {organisation.name}
            {invitation.role && ` as ${aRole(invitation.role)}`}. Its members see everything in it.
            {me.organisations.length > 0 && " You'll stay in your other organisations."}
          </p>
          {refusal && (
            <div role="alert" className="border-destructive/50 rounded-md border px-3 py-2 text-sm">
              <p className="text-destructive">{refusal}</p>
            </div>
          )}
          <Button className="w-full" disabled={accepting} onClick={onAccept}>
            Accept
          </Button>
        </>
      );
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-xl" role="heading" aria-level={1}>
            {title}
          </CardTitle>
          {invitation && <CardDescription>An invitation to an organisation on aios.</CardDescription>}
        </CardHeader>
        <CardContent className="flex flex-col gap-3">{body}</CardContent>
      </Card>
    </div>
  );
}

/** A 404: the link has expired, been revoked or never existed. */
function gone(err: unknown): null {
  if (err instanceof ApiError && err.status === 404) return null;
  throw err;
}
