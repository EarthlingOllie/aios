import { useCallback, useEffect, useState } from "react";
import { LogOut } from "lucide-react";

import { Chat } from "@/components/Chat";
import { Documents } from "@/components/Documents";
import { Login } from "@/components/Login";
import { Button } from "@/components/ui/button";
import { api, ApiError, describeError, type User } from "@/lib/api";

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
