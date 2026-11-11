import { useCallback, useEffect, useState } from "react";
import { Navigate, NavLink, Outlet, Route, Routes, useLocation, useMatch, useSearchParams } from "react-router";
import {
  Bot,
  Building2,
  CalendarClock,
  ChartLine,
  Database,
  LogOut,
  MessagesSquare,
  Search,
  Settings,
  Sparkles,
  Workflow,
  type LucideIcon,
} from "lucide-react";

import { Documents, DocumentsProvider } from "@/components/Documents";
import { NotificationsMenu, NotificationsProvider } from "@/components/Notifications";
import { Button } from "@/components/ui/button";
import { api, ApiError, describeError, fullName, onSessionEnded, type Me } from "@/lib/api";
import { nextPath, signInPath, SIGNED_OUT_PATHS } from "@/lib/auth";
import { CurrentUserContext, ReloadCurrentUserContext, SetCurrentUserContext } from "@/lib/current-user";
import {
  inOrganisation,
  lastOrganisation,
  organisationBase,
  organisationToOpen,
  rememberOrganisation,
} from "@/lib/organisation-path";
import { cn } from "@/lib/utils";
import { ChatDetail, ChatForm, ChatsIndex, ChatsLanding, ChatsSection } from "@/pages/Chats";
import {
  AgentDetail,
  AgentForm,
  AgentsIndex,
  AgentsSection,
  AgentVersionPage,
} from "@/pages/Agents";
import { LoginPage, RegisterPage } from "@/pages/Auth";
import { DataPage } from "@/pages/Data";
import { InviteRoute } from "@/pages/Invite";
import { OrganisationPage } from "@/pages/Organisation";
import { RunPage } from "@/pages/Run";
import { SchedulerPage } from "@/pages/Scheduler";
import { SearchPage } from "@/pages/Search";
import { SettingsPage } from "@/pages/Settings";
import {
  SkillDetail,
  SkillForm,
  SkillsIndex,
  SkillsSection,
  SkillVersionPage,
} from "@/pages/Skills";
import { StubPage } from "@/pages/Stub";
import { WorkflowRunPage } from "@/pages/WorkflowRun";
import {
  WorkflowDetail,
  WorkflowEditorPage,
  WorkflowsIndex,
  WorkflowsSection,
  WorkflowVersionPage,
} from "@/pages/Workflows";

type AuthState =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "error"; message: string }
  | { status: "signed-in"; user: Me };

/**
 * `organisation` is the slug of the organisation the page was opened in (`/o/<slug>/...`, the
 * router's basename), or null for a page opened without one: signed in, that opens the same page
 * in the organisation used last (an old bookmark), unless it's an invitation link's.
 */
export default function App({ organisation = null }: { organisation?: string | null }) {
  const [auth, setAuth] = useState<AuthState>({ status: "loading" });

  const loadMe = useCallback(() => {
    api
      .me()
      .then((user) => setAuth({ status: "signed-in", user }))
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) setAuth({ status: "anonymous" });
        // Anything else (502 during an API restart, 500, network error) is transient: offer a retry.
        else setAuth({ status: "error", message: describeError(err) });
      });
  }, []);

  useEffect(loadMe, [loadMe]);
  // After a profile edit: the user as the API now has them.
  const setUser = useCallback((user: Me) => setAuth({ status: "signed-in", user }), []);
  const signedIn = auth.status === "signed-in";
  useEffect(() => {
    if (!signedIn) return;
    // Signed out from elsewhere (e.g. a password change in another browser): to sign in, then
    // back here. A fresh page load, as signing out is, so nothing of this user's is left in
    // memory; once, however many requests found out.
    let ended = false;
    onSessionEnded(() => {
      if (ended) return;
      ended = true;
      const { pathname, search, hash } = window.location;
      window.location.assign(signInPath(pathname, search, hash));
    });
    return () => onSessionEnded();
  }, [signedIn]);
  const location = useLocation();
  const onSignedOutPage = SIGNED_OUT_PATHS.includes(location.pathname);
  const user = auth.status === "signed-in" ? auth.user : null;
  const member = user !== null && organisation !== null && user.organisations.some((o) => o.slug === organisation);
  useEffect(() => {
    if (member && organisation !== null) rememberOrganisation(organisation);
  }, [member, organisation]);
  // Signed in on a page opened without an organisation: the same page in the one used last.
  const opening =
    user !== null && organisation === null && !onSignedOutPage && !location.pathname.startsWith("/invite/")
      ? organisationToOpen(user.organisations, lastOrganisation())
      : null;

  if (auth.status === "loading") return null;
  if (auth.status === "anonymous" && organisation !== null) {
    // Signing in is no organisation's page: the sign-in page itself, coming back to this one.
    const { pathname, search, hash } = window.location;
    return <PageLoad to={signInPath(pathname, search, hash)} />;
  }
  if (auth.status === "anonymous") {
    const onSignedIn = (user: Me) => setAuth({ status: "signed-in", user });
    return (
      <Routes>
        <Route path="login" element={<LoginPage onSignedIn={onSignedIn} />} />
        <Route path="register" element={<RegisterPage onSignedIn={onSignedIn} />} />
        {/* An invitation link opens signed out too: its page offers registering through it. */}
        <Route
          path="invite/:token"
          element={
            <div className="h-svh">
              <InviteRoute />
            </div>
          }
        />
        {/* Anywhere else: sign in first, then come back. */}
        <Route
          path="*"
          element={<Navigate to={signInPath(location.pathname, location.search, location.hash)} replace />}
        />
      </Routes>
    );
  }
  if (auth.status === "error") {
    return (
      <div className="flex min-h-svh flex-col items-center justify-center gap-3 p-4 text-center">
        <p className="text-sm font-medium">Couldn't reach the server.</p>
        <p className="max-w-sm truncate text-xs text-muted-foreground">{auth.message}</p>
        <Button
          variant="outline"
          onClick={() => {
            setAuth({ status: "loading" });
            loadMe();
          }}
        >
          Retry
        </Button>
      </div>
    );
  }

  // Signed in on the sign-in or registration page (just now, or by visiting it): on to where they
  // were headed.
  if (onSignedOutPage) return <AfterSignIn />;
  // A new page load, as the organisation is the router's basename.
  if (opening !== null) return <PageLoad to={inOrganisation(opening, location.pathname + location.search + location.hash)} />;
  // A link to an organisation the user isn't in, or that doesn't exist: alike, so a link can't
  // tell which.
  if (organisation !== null && !member) return <OrganisationNotFound user={auth.user} />;

  // A fresh page load, so nothing of this user's is left in memory for whoever signs in next.
  const signOut = () => api.logout().then(() => window.location.assign("/login"));
  return (
    <Routes>
      {/* Documents are held above every page, so the Chats section's panel and the Data page
          share one list, and an upload in flight survives leaving either. */}
      <Route
        element={
          <DocumentsProvider>
            <NotificationsProvider>
              <CurrentUserContext.Provider value={auth.user}>
                <SetCurrentUserContext.Provider value={setUser}>
                  <ReloadCurrentUserContext.Provider value={loadMe}>
                    <Shell user={auth.user} onSignOut={signOut} />
                  </ReloadCurrentUserContext.Provider>
                </SetCurrentUserContext.Provider>
              </CurrentUserContext.Provider>
            </NotificationsProvider>
          </DocumentsProvider>
        }
      >
        {/* One place to chat: `/` opens the Chats section. */}
        <Route index element={<ChatsLanding />} />
        <Route path="skills" element={<SkillsSection />}>
          <Route index element={<SkillsIndex />} />
          <Route path="new" element={<SkillForm />} />
          <Route path=":skillId/:tab?" element={<SkillDetail />} />
          <Route path=":skillId/edit" element={<SkillForm />} />
          <Route path=":skillId/versions/:version" element={<SkillVersionPage />} />
        </Route>
        <Route path="search" element={<SearchPage />} />
        <Route path="agents" element={<AgentsSection />}>
          <Route index element={<AgentsIndex />} />
          <Route path="new" element={<AgentForm />} />
          <Route path=":agentId/:tab?" element={<AgentDetail />} />
          <Route path=":agentId/edit" element={<AgentForm />} />
          <Route path=":agentId/versions/:version" element={<AgentVersionPage />} />
          <Route path=":agentId/runs/:runId" element={<RunPage />} />
        </Route>
        <Route path="workflows" element={<WorkflowsSection />}>
          <Route index element={<WorkflowsIndex />} />
          <Route path="new" element={<WorkflowEditorPage />} />
          <Route path=":workflowId/:tab?" element={<WorkflowDetail />} />
          <Route path=":workflowId/edit" element={<WorkflowEditorPage />} />
          <Route path=":workflowId/runs/:runId" element={<WorkflowRunPage />} />
          <Route path=":workflowId/versions/:version" element={<WorkflowVersionPage />} />
        </Route>
        <Route path="scheduler" element={<SchedulerPage />} />
        <Route path="data/:tab?" element={<DataPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="organisation/:tab?" element={<OrganisationPage />} />
        <Route path="invite/:token" element={<InviteRoute />} />
        <Route path="chats" element={<ChatsSection />}>
          <Route index element={<ChatsIndex />} />
          <Route path="new" element={<ChatForm />} />
          <Route path=":chatId/:tab?" element={<ChatDetail />} />
          <Route path=":chatId/edit" element={<ChatForm />} />
        </Route>
        {navItems
          .filter((item): item is NavItem & { stub: string } => Boolean(item.stub))
          .map(({ to, label, icon, stub }) => (
            <Route key={to} path={to} element={<StubPage title={label} icon={icon} description={stub} />} />
          ))}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

/** Replaces the page with a new load of `to`: for a page outside the router's basename. */
function PageLoad({ to }: { to: string }) {
  useEffect(() => window.location.replace(to), [to]);
  return null;
}

function OrganisationNotFound({ user }: { user: Me }) {
  const yours = organisationToOpen(user.organisations, lastOrganisation());
  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-3 p-4 text-center">
      <h1 className="text-lg font-medium">Organisation not found</h1>
      <p className="max-w-sm text-sm text-muted-foreground">
        This link is to an organisation that doesn't exist, or that you're not a member of.
      </p>
      {yours !== null && (
        // A new page load: the organisation is the router's basename.
        <Button variant="outline" asChild>
          <a href={`${organisationBase(yours)}/`}>Go to your organisation</a>
        </Button>
      )}
    </div>
  );
}

function AfterSignIn() {
  const [params] = useSearchParams();
  return <Navigate to={nextPath(params.get("next"))} replace />;
}

type NavItem = {
  to: string;
  label: string;
  icon: LucideIcon;
  end: boolean;
  /** When set, the item is routed to a placeholder page with this description until it's built. */
  stub?: string;
};

const navItems: NavItem[] = [
  { to: "/search", label: "Search", icon: Search, end: false },
  {
    to: "/insights",
    label: "Insights",
    icon: ChartLine,
    end: false,
    stub: "See how your agents are used and how they perform.",
  },
  { to: "/chats", label: "Chats", icon: MessagesSquare, end: false },
  { to: "/agents", label: "Agents", icon: Bot, end: false },
  { to: "/skills", label: "Skills", icon: Sparkles, end: false },
  { to: "/workflows", label: "Workflows", icon: Workflow, end: false },
  { to: "/scheduler", label: "Scheduler", icon: CalendarClock, end: false },
  { to: "/data", label: "Data", icon: Database, end: false },
  { to: "/settings", label: "Settings", icon: Settings, end: false },
];

function Shell({ user, onSignOut }: { user: Me; onSignOut: () => void }) {
  // `/` too, which lands in the Chats section: the panel stays mounted through the redirect.
  const onLanding = useMatch("/") !== null;
  const onChats = useMatch("/chats/*") !== null || onLanding;
  const nav = (className: string) =>
    navItems.map(({ to, label, icon: Icon, end }) => (
      <NavLink
        key={to}
        to={to}
        end={end}
        className={({ isActive }) =>
          cn(
            "hover:bg-accent flex items-center gap-2 rounded-md px-2 py-1.5 text-sm",
            isActive ? "bg-accent font-medium" : "text-muted-foreground",
            className,
          )
        }
      >
        <Icon className="size-4" />
        {label}
      </NavLink>
    ));
  // Whose things every page shows: the user's organisation, which opens its page.
  const organisation = (className: string) => (
    <NavLink
      to="/organisation"
      className={({ isActive }) =>
        cn(
          "hover:bg-accent flex min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-sm",
          isActive ? "bg-accent font-medium" : "text-muted-foreground",
          className,
        )
      }
    >
      <Building2 className="size-4 shrink-0" />
      <span className="truncate">{user.organisation.name}</span>
    </NavLink>
  );

  return (
    <div className="grid h-svh grid-cols-1 grid-rows-[auto_1fr] md:grid-cols-[260px_1fr] md:grid-rows-1">
      <aside className="hidden min-h-0 flex-col gap-4 border-r p-4 md:flex">
        <div className="flex items-center gap-2">
          <span className="flex-1 truncate text-sm font-medium">{fullName(user)}</span>
          <NotificationsMenu />
          <Button variant="ghost" size="icon" aria-label="Sign out" onClick={onSignOut}>
            <LogOut />
          </Button>
        </div>
        {organisation("")}
        <nav className="flex flex-col gap-0.5">{nav("")}</nav>
        {/* Documents ground the chats' documents assistant, so they live alongside it. */}
        {onChats && (
          <div className="min-h-0 flex-1 border-t pt-4">
            <Documents />
          </div>
        )}
      </aside>
      <nav className="flex items-center gap-1 border-b px-2 py-1.5 md:hidden">
        {/* Only the links scroll sideways, so Sign out stays in view however many there are. */}
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
          {nav("shrink-0")}
          {organisation("shrink-0")}
        </div>
        <NotificationsMenu className="shrink-0" />
        <Button variant="ghost" size="icon" className="shrink-0" aria-label="Sign out" onClick={onSignOut}>
          <LogOut />
        </Button>
      </nav>
      <main className="min-h-0">
        <Outlet />
      </main>
    </div>
  );
}
