import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { Bell } from "lucide-react";

import { formatDate, useLoad, usePoll, useVisible } from "@/components/crud";
import { Button } from "@/components/ui/button";
import { api, describeError, type Notification, type NotificationList } from "@/lib/api";
import { badgeText, describeNotification } from "@/lib/notifications";
import { cn } from "@/lib/utils";

/** How often the signed-in user's notifications are read again (ms). */
export const NOTIFICATIONS_POLL_MS = 10_000;

type NotificationsState = {
  data: NotificationList | undefined;
  error: string | undefined;
  /** Read them again now, e.g. after something that clears one (deciding a request). */
  reload: () => Promise<void>;
};

const NotificationsContext = createContext<NotificationsState | null>(null);

/**
 * Polls for the signed-in user's notifications, for every `NotificationsMenu` below it (the
 * navigation has one for wide screens and one for narrow), so they're read once however many
 * show them. Not while the tab is hidden: it asks again as soon as it's shown.
 */
export function NotificationsProvider({ children }: { children: ReactNode }) {
  const { data, error, reload } = useLoad(useCallback(() => api.listNotifications(), []));
  const visible = useVisible();
  const wasVisible = useRef(visible);
  useEffect(() => {
    // Shown again: what changed meanwhile, now (the first load is useLoad's).
    if (visible && !wasVisible.current) void reload();
    wasVisible.current = visible;
  }, [visible, reload]);
  usePoll(visible, reload, NOTIFICATIONS_POLL_MS);
  return (
    <NotificationsContext.Provider value={{ data, error, reload }}>{children}</NotificationsContext.Provider>
  );
}

const noRefresh = () => Promise.resolve();

/** Reads the notifications again now; does nothing outside a `NotificationsProvider`. */
export function useRefreshNotifications(): () => Promise<void> {
  return useContext(NotificationsContext)?.reload ?? noRefresh;
}

/**
 * The navigation's bell: an unread badge, opening a list of the user's notifications, unread
 * first. An item opens what it's about; an unread one can be marked read.
 */
export function NotificationsMenu({ className }: { className?: string }) {
  const state = useContext(NotificationsContext);
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const unread = state?.data?.unread ?? 0;
  const badge = badgeText(unread);

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

  return (
    <div ref={root} className={cn("relative", className)}>
      <Button
        variant="ghost"
        size="icon"
        className="relative"
        aria-label={unread > 0 ? `Notifications, ${unread} unread` : "Notifications"}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Bell />
        {badge && (
          <span
            aria-hidden
            className="bg-destructive absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[10px] leading-none font-semibold text-white"
          >
            {badge}
          </span>
        )}
      </Button>
      {open && (
        <NotificationsPanel
          data={state?.data}
          error={state?.error}
          reload={state?.reload ?? noRefresh}
          onNavigate={() => setOpen(false)}
        />
      )}
    </div>
  );
}

function NotificationsPanel({
  data,
  error,
  reload,
  onNavigate,
}: {
  data: NotificationList | undefined;
  error: string | undefined;
  reload: () => Promise<void>;
  onNavigate: () => void;
}) {
  const [markError, setMarkError] = useState<string>();
  const markRead = async (notification: Notification) => {
    setMarkError(undefined);
    try {
      await api.markNotificationRead(notification.id);
      await reload();
    } catch (err) {
      setMarkError(describeError(err));
    }
  };
  const shownError = markError ?? error;
  return (
    <section
      aria-label="Notifications"
      className="bg-background absolute top-full right-0 z-50 mt-1 flex max-h-96 w-80 max-w-[calc(100vw-1rem)] flex-col overflow-y-auto rounded-md border text-sm shadow-md md:right-auto md:left-0"
    >
      <h2 className="border-b px-3 py-2 text-sm font-semibold">Notifications</h2>
      {shownError && <p className="text-destructive px-3 py-2 text-xs">{shownError}</p>}
      {data && data.notifications.length === 0 && (
        <p className="text-muted-foreground px-3 py-3">Nothing yet. When an agent needs you, it shows here.</p>
      )}
      <ul className="divide-y">
        {data?.notifications.map((n) => (
          <NotificationItem key={n.id} notification={n} onNavigate={onNavigate} onMarkRead={() => markRead(n)} />
        ))}
      </ul>
    </section>
  );
}

function NotificationItem({
  notification,
  onNavigate,
  onMarkRead,
}: {
  notification: Notification;
  onNavigate: () => void;
  onMarkRead: () => void;
}) {
  const view = describeNotification(notification);
  const unread = !notification.read_at;
  const body = (
    <>
      {unread && <span className="sr-only">Unread: </span>}
      <span className={cn("block", unread ? "font-medium" : "text-muted-foreground")}>{view.heading}</span>
      {view.detail && <span className="text-muted-foreground block truncate text-xs">{view.detail}</span>}
      <span className="text-muted-foreground block text-xs">{formatDate(notification.created_at)}</span>
    </>
  );
  return (
    <li className={cn("flex items-start gap-2 px-3 py-2", unread && "bg-accent/40")}>
      {unread && <span aria-hidden className="bg-primary mt-1.5 size-2 shrink-0 rounded-full" />}
      {view.to ? (
        <Link to={view.to} onClick={onNavigate} className="min-w-0 flex-1 hover:underline">
          {body}
        </Link>
      ) : (
        <div className="min-w-0 flex-1">{body}</div>
      )}
      {unread && (
        <Button variant="ghost" size="sm" className="h-auto shrink-0 px-2 py-1 text-xs" onClick={onMarkRead}>
          Mark read
        </Button>
      )}
    </li>
  );
}
