import { useCallback, useEffect, useState } from "react";
import { Navigate, NavLink, Outlet, Route, Routes, useMatch } from "react-router";
import {
  Bot,
  CalendarClock,
  ChartLine,
  LogOut,
  MessageSquare,
  Search,
  Settings,
  Sparkles,
  Workflow,
  type LucideIcon,
} from "lucide-react";

import { Chat } from "@/components/Chat";
import { Documents } from "@/components/Documents";
import { Login } from "@/components/Login";
import { Button } from "@/components/ui/button";
import { api, ApiError, describeError, type User } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  AgentDetail,
  AgentForm,
  AgentsIndex,
  AgentsSection,
  AgentVersionPage,
} from "@/pages/Agents";
import { RunPage } from "@/pages/Run";
import { SearchPage } from "@/pages/Search";
import {
  SkillDetail,
  SkillForm,
  SkillsIndex,
  SkillsSection,
  SkillVersionPage,
} from "@/pages/Skills";
import { StubPage } from "@/pages/Stub";
import {
  NewWorkflowForm,
  WorkflowDetail,
  WorkflowsIndex,
  WorkflowsSection,
  WorkflowVersionPage,
} from "@/pages/Workflows";

type AuthState =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "error"; message: string }
  | { status: "signed-in"; user: User };

export default function App() {
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

  if (auth.status === "loading") return null;
  if (auth.status === "anonymous") return <Login />;
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

  const signOut = () => api.logout().then(() => setAuth({ status: "anonymous" }));
  return (
    <Routes>
      <Route element={<Shell user={auth.user} onSignOut={signOut} />}>
        {/* Chat is rendered by Shell so it stays mounted across navigation. */}
        <Route index element={null} />
        <Route path="skills" element={<SkillsSection />}>
          <Route index element={<SkillsIndex />} />
          <Route path="new" element={<SkillForm />} />
          <Route path=":skillId" element={<SkillDetail />} />
          <Route path=":skillId/edit" element={<SkillForm />} />
          <Route path=":skillId/versions/:version" element={<SkillVersionPage />} />
        </Route>
        <Route path="search" element={<SearchPage />} />
        <Route path="agents" element={<AgentsSection />}>
          <Route index element={<AgentsIndex />} />
          <Route path="new" element={<AgentForm />} />
          <Route path=":agentId" element={<AgentDetail />} />
          <Route path=":agentId/edit" element={<AgentForm />} />
          <Route path=":agentId/versions/:version" element={<AgentVersionPage />} />
          <Route path=":agentId/runs/:runId" element={<RunPage />} />
        </Route>
        <Route path="workflows" element={<WorkflowsSection />}>
          <Route index element={<WorkflowsIndex />} />
          <Route path="new" element={<NewWorkflowForm />} />
          <Route path=":workflowId" element={<WorkflowDetail />} />
          <Route path=":workflowId/versions/:version" element={<WorkflowVersionPage />} />
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
  { to: "/", label: "Chat", icon: MessageSquare, end: true },
  { to: "/agents", label: "Agents", icon: Bot, end: false },
  { to: "/skills", label: "Skills", icon: Sparkles, end: false },
  { to: "/workflows", label: "Workflows", icon: Workflow, end: false },
  { to: "/scheduler", label: "Scheduler", icon: CalendarClock, end: false, stub: "Run agents on a schedule." },
  { to: "/settings", label: "Settings", icon: Settings, end: false, stub: "Manage your account and workspace." },
];

function Shell({ user, onSignOut }: { user: User; onSignOut: () => void }) {
  const onChat = useMatch("/") !== null;
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

  return (
    <div className="grid h-svh grid-cols-1 grid-rows-[auto_1fr] md:grid-cols-[260px_1fr] md:grid-rows-1">
      <aside className="hidden min-h-0 flex-col gap-4 border-r p-4 md:flex">
        <div className="flex items-center gap-2">
          {user.avatar_url && <img src={user.avatar_url} alt="" className="size-7 rounded-full" />}
          <span className="flex-1 truncate text-sm font-medium">{user.name ?? user.login}</span>
          <Button variant="ghost" size="icon" aria-label="Sign out" onClick={onSignOut}>
            <LogOut />
          </Button>
        </div>
        <nav className="flex flex-col gap-0.5">{nav("")}</nav>
        {/* Documents ground the chat assistant, so they live alongside it. */}
        {onChat && (
          <div className="min-h-0 flex-1 border-t pt-4">
            <Documents />
          </div>
        )}
      </aside>
      <nav className="flex items-center gap-1 border-b px-2 py-1.5 md:hidden">
        {/* Only the links scroll sideways, so Sign out stays in view however many there are. */}
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">{nav("shrink-0")}</div>
        <Button variant="ghost" size="icon" className="shrink-0" aria-label="Sign out" onClick={onSignOut}>
          <LogOut />
        </Button>
      </nav>
      <main className="min-h-0">
        {/* Kept mounted (just hidden) off the chat route, so an in-progress conversation
            and any streaming reply survive a trip to another section. */}
        <div className={cn("h-full", !onChat && "hidden")}>
          <Chat />
        </div>
        <Outlet />
      </main>
    </div>
  );
}
