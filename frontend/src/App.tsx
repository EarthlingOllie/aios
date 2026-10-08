import { useEffect, useState } from "react";
import { LogOut } from "lucide-react";

import { Chat } from "@/components/Chat";
import { Documents } from "@/components/Documents";
import { Login } from "@/components/Login";
import { Button } from "@/components/ui/button";
import { api, ApiError, type User } from "@/lib/api";

type AuthState = { status: "loading" } | { status: "anonymous" } | { status: "signed-in"; user: User };

export default function App() {
  const [auth, setAuth] = useState<AuthState>({ status: "loading" });

  useEffect(() => {
    api
      .me()
      .then((user) => setAuth({ status: "signed-in", user }))
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) setAuth({ status: "anonymous" });
        else throw err;
      });
  }, []);

  if (auth.status === "loading") return null;
  if (auth.status === "anonymous") return <Login />;

  const { user } = auth;
  return (
    <div className="grid h-svh grid-cols-1 md:grid-cols-[280px_1fr]">
      <aside className="hidden flex-col gap-4 border-r p-4 md:flex">
        <div className="flex items-center gap-2">
          {user.avatar_url && <img src={user.avatar_url} alt="" className="size-7 rounded-full" />}
          <span className="flex-1 truncate text-sm font-medium">{user.name ?? user.login}</span>
          <Button
            variant="ghost"
            size="icon"
            aria-label="Sign out"
            onClick={() => api.logout().then(() => setAuth({ status: "anonymous" }))}
          >
            <LogOut />
          </Button>
        </div>
        <Documents />
      </aside>
      <main className="min-h-0">
        <Chat />
      </main>
    </div>
  );
}
