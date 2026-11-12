import { useCallback, useEffect, useRef, useState } from "react";
import { Link, Navigate, NavLink, Outlet, Route, Routes, useLocation, useMatch, useSearchParams } from "react-router";
import {
  Bot,
  Building2,
  CalendarClock,
  Check,
  ChevronsUpDown,
  Plus,
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

import { formatDate } from "@/components/crud";
import { Documents, DocumentsProvider } from "@/components/Documents";
import { JevNotice } from "@/components/JevNotice";
import { NotificationsMenu, NotificationsProvider } from "@/components/Notifications";
import { PersonStreamProvider } from "@/components/PersonStream";
import { Button } from "@/components/ui/button";
import {
  api,
  ApiError,
  describeError,
  fullName,
  onSessionEnded,
  openOrganisationApi,
  type Me,
  type Membership,
} from "@/lib/api";
import { nextPath, signInPath, SIGNED_OUT_PATHS } from "@/lib/auth";
import {
  CurrentOrganisationContext,
  CurrentUserContext,
  ReloadCurrentUserContext,
  SetCurrentUserContext,
} from "@/lib/current-user";
import {
  inOrganisation,
  organisationBase,
  organisationToOpen,
  recentOrganisations,
  rememberOrganisation,
  WELCOME,
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
import { SourcePage } from "@/pages/SourcePage";
import { ConnectorPage } from "@/pages/ConnectorPage";
import { InsightsPage } from "@/pages/Insights";
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
import { WelcomePage } from "@/pages/Welcome";
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
 * in the organisation used most recently (an old bookmark), unless it's an invitation link's or
 * the welcome page. A person in no organisation is sent to the welcome page (`/welcome`), which
 * creates one.
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
  const membership = organisation === null ? undefined : user?.organisations.find((o) => o.slug === organisation);
  const member = membership !== undefined;
  const onWelcome = location.pathname === WELCOME;
  const onInvite = location.pathname.startsWith("/invite/");
  // Before any page asks: the API's prefix is the organisation's id (its slug is the link's). A
  // page opened without one (an invitation link's) is shown in the one used most recently.
  const apiOrganisation =
    membership ??
    (user !== null && organisation === null
      ? user.organisations.find((o) => o.slug === organisationToOpen(user.organisations, recentOrganisations()))
      : undefined);
  if (apiOrganisation !== undefined) openOrganisationApi(apiOrganisation.id);
  useEffect(() => {
    if (member && organisation !== null) rememberOrganisation(organisation);
  }, [member, organisation]);
  // Signed in on a page opened without an organisation: the same page in the one used most
  // recently (none for a person in none, who goes to the welcome page).
  const opening =
    user !== null && organisation === null && !onSignedOutPage && !onInvite && !onWelcome
      ? organisationToOpen(user.organisations, recentOrganisations())
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

  // A fresh page load, so nothing of this user's is left in memory for whoever signs in next.
  const signOut = () => api.logout().then(() => window.location.assign("/login"));

  // Signed in on the sign-in or registration page (just now, or by visiting it): on to where they
  // were headed.
  if (onSignedOutPage) return <AfterSignIn />;
  if (organisation === null && onWelcome) return <WelcomePage me={auth.user} onSignOut={signOut} />;
  if (organisation === null && !onInvite && opening === null) return <Navigate to={WELCOME} replace />;
  // A new page load, as the organisation is the router's basename.
  if (opening !== null) return <PageLoad to={inOrganisation(opening, location.pathname + location.search + location.hash)} />;
  // A link to an organisation the user isn't in, or that doesn't exist: alike, so a link can't
  // tell which. A person in none (the one they were in was deleted, say) lands on the welcome page.
  if (organisation !== null && !member && auth.user.organisations.length === 0) return <PageLoad to={WELCOME} />;
  if (organisation !== null && !member) return <OrganisationNotFound user={auth.user} />;
  if (apiOrganisation === undefined) {
    // An invitation link's page for a person in no organisation: no shell, as signed out.
    return (
      <CurrentUserContext.Provider value={auth.user}>
        <ReloadCurrentUserContext.Provider value={loadMe}>
          <div className="h-svh">
            <Routes>
              <Route path="invite/:token" element={<InviteRoute />} />
            </Routes>
          </div>
        </ReloadCurrentUserContext.Provider>
      </CurrentUserContext.Provider>
    );
  }

  return (
    <Routes>
      {/* Documents are held above every page, so the Chats section's panel and the Data page
          share one list, and an upload in flight survives leaving either. */}
      <Route
        element={
          <DocumentsProvider>
            <PersonStreamProvider>
              <NotificationsProvider>
                <CurrentUserContext.Provider value={auth.user}>
                  <CurrentOrganisationContext.Provider value={membership ?? null}>
                    <SetCurrentUserContext.Provider value={setUser}>
                      <ReloadCurrentUserContext.Provider value={loadMe}>
                        <Shell user={auth.user} current={apiOrganisation} onSignOut={signOut} />
                      </ReloadCurrentUserContext.Provider>
                    </SetCurrentUserContext.Provider>
                  </CurrentOrganisationContext.Provider>
                </CurrentUserContext.Provider>
              </NotificationsProvider>
            </PersonStreamProvider>
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
        <Route path="insights" element={<InsightsPage />} />
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
        <Route path="data/sources/:sourceId" element={<SourcePage />} />
        <Route path="data/connectors/:connectorId" element={<ConnectorPage />} />
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
  const yours = organisationToOpen(user.organisations, recentOrganisations());
  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-3 p-4 text-center">
      <h1 className="text-lg font-medium">Organisation not found</h1>
      <p className="max-w-sm text-sm text-muted-foreground">
        This link is to an organisation that doesn't exist, or that you're not a member of.
      </p>
      {/* New page loads: the organisation is the router's basename. */}
      <div className="flex flex-wrap justify-center gap-2">
        {yours !== null && (
          <Button variant="outline" asChild>
            <a href={`${organisationBase(yours)}/`}>Go to your organisation</a>
          </Button>
        )}
        <Button variant={yours === null ? "outline" : "ghost"} asChild>
          <a href={WELCOME}>Create an organisation</a>
        </Button>
      </div>
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
  { to: "/insights", label: "Insights", icon: ChartLine, end: false },
  { to: "/chats", label: "Chats", icon: MessagesSquare, end: false },
  { to: "/agents", label: "Agents", icon: Bot, end: false },
  { to: "/skills", label: "Skills", icon: Sparkles, end: false },
  { to: "/workflows", label: "Workflows", icon: Workflow, end: false },
  { to: "/scheduler", label: "Scheduler", icon: CalendarClock, end: false },
  { to: "/data", label: "Data", icon: Database, end: false },
  { to: "/settings", label: "Settings", icon: Settings, end: false },
];

function Shell({ user, current, onSignOut }: { user: Me; current: Membership; onSignOut: () => void }) {
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
  // Whose things every page shows: the organisation the page is open in, as the switcher.
  const organisation = (className: string) => (
    <OrganisationSwitcher memberships={user.organisations} current={current} className={className} />
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
        {/* Outside the links, which scroll sideways (so Sign out stays in view however many there
            are), so its menu isn't clipped by them. */}
        {organisation("max-w-40 shrink-0")}
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">{nav("shrink-0")}</div>
        <NotificationsMenu className="shrink-0" />
        <Button variant="ghost" size="icon" className="shrink-0" aria-label="Sign out" onClick={onSignOut}>
          <LogOut />
        </Button>
      </nav>
      <main className="flex min-h-0 flex-col">
        {current.deletes_at && <DeletionBanner name={current.name} deletesAt={current.deletes_at} />}
        <JevNotice />
        <div className="min-h-0 flex-1">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

/** On every page of an organisation scheduled for deletion: when it goes. */
function DeletionBanner({ name, deletesAt }: { name: string; deletesAt: string }) {
  return (
    <div
      role="status"
      aria-label="Deletion scheduled"
      className="border-destructive/30 bg-destructive/10 flex flex-wrap items-center gap-x-2 border-b px-4 py-2 text-sm"
    >
      <span>
        {name} will be deleted on {formatDate(deletesAt)}, with everything in it.
      </span>
      <Link to="/organisation/settings" className="underline">
        Organisation settings
      </Link>
    </div>
  );
}

/**
 * The shell's organisation: its name, opening a menu of every organisation the user is in (the
 * one joined most recently first), with "Create an organisation" (the welcome page). The one the
 * page is open in opens its Organisation page; another opens in its own link, a new page load
 * (the organisation is the router's basename).
 */
function OrganisationSwitcher({
  memberships,
  current,
  className,
}: {
  memberships: Membership[];
  current: Membership;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const onOrganisationPage = useMatch("/organisation/*") !== null;

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const item = "hover:bg-accent flex min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-sm";
  return (
    <div ref={root} className={cn("relative min-w-0", className)}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className={cn(
          item,
          "w-full",
          onOrganisationPage ? "bg-accent font-medium" : "text-muted-foreground",
        )}
      >
        <Building2 className="size-4 shrink-0" />
        <span className="flex-1 truncate text-left">{current.name}</span>
        <ChevronsUpDown aria-hidden className="size-3.5 shrink-0 opacity-60" />
      </button>
      {open && (
        <div
          role="menu"
          aria-label="Organisations"
          className="bg-popover text-popover-foreground absolute top-full left-0 z-50 mt-1 flex w-64 max-w-[calc(100vw-2rem)] flex-col gap-0.5 rounded-md border p-1 shadow-md"
        >
          {memberships.map((m) =>
            m.id === current.id ? (
              <Link
                key={m.id}
                role="menuitem"
                to="/organisation"
                aria-current="true"
                className={cn(item, "font-medium")}
                onClick={() => setOpen(false)}
              >
                <Check aria-hidden className="size-4 shrink-0" />
                <span className="truncate">{m.name}</span>
              </Link>
            ) : (
              <a key={m.id} role="menuitem" href={`${organisationBase(m.slug)}/`} className={item}>
                <span aria-hidden className="size-4 shrink-0" />
                <span className="truncate">{m.name}</span>
              </a>
            ),
          )}
          <div role="separator" className="bg-border my-1 h-px" />
          <a role="menuitem" href={WELCOME} className={cn(item, "text-muted-foreground")}>
            <Plus aria-hidden className="size-4 shrink-0" />
            Create an organisation
          </a>
        </div>
      )}
    </div>
  );
}
