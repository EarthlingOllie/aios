import { Link } from "react-router";

import { Section } from "@/components/crud";
import { useCurrentUser } from "@/lib/current-user";

/** The user's own settings: so far, only where the organisation's are. */
export function SettingsPage() {
  const me = useCurrentUser();

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6 md:px-8">
        <header>
          <h1 className="text-xl font-semibold">Settings</h1>
          <p className="text-muted-foreground mt-1 text-sm">Manage your account.</p>
        </header>
        {me && (
          <Section title="Organisation">
            <p className="text-muted-foreground text-sm">
              You're in <span className="text-foreground font-medium">{me.organisation.name}</span>.
              Its name, members and run history export are on the{" "}
              <Link to="/organisation" className="text-foreground underline underline-offset-4">
                Organisation
              </Link>{" "}
              page.
            </p>
          </Section>
        )}
      </div>
    </div>
  );
}
