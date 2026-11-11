import type { Notification } from "@/lib/api";
import { inOrganisation } from "@/lib/organisation-path";

export type NotificationView = {
  heading: string;
  detail: string | null;
  /** Where clicking it goes, in its organisation: the thing it's about, or nowhere for a kind
   * this app doesn't know. */
  to: string | null;
  /** The slug of the organisation it's from: it opens there. */
  organisation: string;
};

/** How a notification shows in the navigation's list. */
export function describeNotification(notification: Notification): NotificationView {
  if (notification.kind === "schema_change_request") {
    return {
      heading: "A schema change is waiting for you",
      detail: notification.title ?? "This request no longer exists.",
      to: `/data/changes?request=${encodeURIComponent(notification.ref.request_id)}`,
      organisation: notification.organisation.slug,
    };
  }
  return {
    heading: notification.title ?? "Notification",
    detail: null,
    to: null,
    organisation: notification.organisation.slug,
  };
}

/**
 * Where a notification opens from a page in the organisation `here` (its slug; null for none):
 * within the page for one from that organisation, else in its own organisation, which is a new
 * page load (the organisation is the router's basename). Null for nowhere.
 */
export function notificationLink(view: NotificationView, here: string | null): { to: string; reload: boolean } | null {
  if (view.to === null) return null;
  if (view.organisation === here) return { to: view.to, reload: false };
  return { to: inOrganisation(view.organisation, view.to), reload: true };
}

/** The unread badge's text: nothing with none unread, and no more than "9+". */
export function badgeText(unread: number): string | null {
  if (unread <= 0) return null;
  return unread > 9 ? "9+" : String(unread);
}
