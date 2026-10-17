import type { Notification } from "@/lib/api";

export type NotificationView = {
  heading: string;
  detail: string | null;
  /** Where clicking it goes: the thing it's about, or nowhere for a kind this app doesn't know. */
  to: string | null;
};

/** How a notification shows in the navigation's list. */
export function describeNotification(notification: Notification): NotificationView {
  if (notification.kind === "schema_change_request") {
    return {
      heading: "A schema change is waiting for you",
      detail: notification.title ?? "This request no longer exists.",
      to: `/data?tab=changes&request=${encodeURIComponent(notification.ref.request_id)}`,
    };
  }
  return { heading: notification.title ?? "Notification", detail: null, to: null };
}

/** The unread badge's text: nothing with none unread, and no more than "9+". */
export function badgeText(unread: number): string | null {
  if (unread <= 0) return null;
  return unread > 9 ? "9+" : String(unread);
}
