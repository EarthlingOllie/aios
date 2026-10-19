import { useCallback, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { Loader2 } from "lucide-react";

import { useLoad } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError, describeError, type Me } from "@/lib/api";
import { useCurrentUser, useReloadCurrentUser } from "@/lib/current-user";

/** The invite page in the shell, for a signed-in user. */
export function InviteRoute() {
  const { token } = useParams();
  return <InvitePage key={token} token={token!} me={useCurrentUser()} />;
}

/**
 * An invitation link's page (`/invite/:token`), signed in or out: which organisation it's for,
 * and Accept, or Sign in (which keeps the token through GitHub's sign-in, and joins a new user
 * on the way back). An expired or revoked link says so; accepting while in another organisation
 * shows the API's reason.
 */
export function InvitePage({ token, me }: { token: string; me: Me | null }) {
  const reloadMe = useReloadCurrentUser();
  const navigate = useNavigate();
  const { data: invitation, error } = useLoad(
    useCallback(() => api.getInvitation(token).catch(gone), [token]),
  );
  const [accepting, setAccepting] = useState(false);
  const [refusal, setRefusal] = useState<string>();

  async function onAccept() {
    setAccepting(true);
    setRefusal(undefined);
    try {
      await api.acceptInvitation(token);
      reloadMe();
      navigate("/organisation");
    } catch (err) {
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
      body = (
        <>
          <p className="text-muted-foreground text-sm">
            You've been invited to join {organisation.name} on aios. Sign in to accept.
          </p>
          <Button asChild className="w-full">
            {/* A plain link: the backend keeps the token through the OAuth round trip. */}
            <a href={`/login?invite=${encodeURIComponent(token)}`}>Sign in with GitHub to accept</a>
          </Button>
        </>
      );
    } else if (me.organisation.id === organisation.id) {
      body = (
        <p className="text-muted-foreground text-sm">
          You're already a member of {organisation.name}. See it on the{" "}
          <Link to="/organisation" className="text-foreground underline underline-offset-4">
            Organisation
          </Link>{" "}
          page.
        </p>
      );
    } else {
      body = (
        <>
          <p className="text-muted-foreground text-sm">
            You've been invited to join {organisation.name}. Its members see everything in it.
          </p>
          {refusal && (
            <div role="alert" className="border-destructive/50 rounded-md border px-3 py-2 text-sm">
              <p className="text-destructive">{refusal}</p>
              <p className="text-muted-foreground mt-1">
                You're in {me.organisation.name}: see who else is in it on the{" "}
                <Link to="/organisation" className="text-foreground underline underline-offset-4">
                  Organisation
                </Link>{" "}
                page.
              </p>
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
