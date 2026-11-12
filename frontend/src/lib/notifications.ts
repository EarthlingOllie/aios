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

/** What a notification's line says while its organisation's store can't be read (its `title` null). */
const HEADINGS: Record<string, string> = {
  mention: "You were mentioned in a chat",
  reply: "A reply in a chat",
  run_failed: "A run failed",
  approval_needed: "An approval is waiting for you",
  released_document_changed: "A document you released has changed",
  released_item_changed: "A released item has changed at its source",
  source_attention: "A source needs attention",
  organisation_deletion_scheduled: "An organisation is scheduled for deletion.",
  organisation_deletion_cancelled: "An organisation will no longer be deleted.",
  budget_warning: "An organisation is nearing its monthly budget",
  budget_reached: "An organisation has reached its monthly budget",
};

/** Where a notification opens, in its organisation: what it's about, or nowhere for a kind this
 * app doesn't know. */
export function notificationPath({ kind, ref }: Notification): string | null {
  const id = (key: string) => encodeURIComponent(String(ref[key]));
  switch (kind) {
    case "approval_needed":
      return `/data/approvals?approval=${id("approval_id")}`;
    case "mention":
    case "reply":
    case "run_failed":
      if (ref.chat_id !== undefined) return `/chats/${id("chat_id")}`;
      if (ref.agent_id !== undefined) return `/agents/${id("agent_id")}/runs/${id("run_id")}`;
      if (ref.workflow_id !== undefined) return `/workflows/${id("workflow_id")}/runs/${id("run_id")}`;
      return null;
    case "released_document_changed":
      return "/data/documents";
    case "released_item_changed":
      return "/organisation/settings";
    case "source_attention":
      return `/data/sources/${id("source_id")}`;
    case "organisation_deletion_scheduled":
      return "/organisation/settings";
    case "organisation_deletion_cancelled":
      return "/organisation";
    case "budget_warning":
    case "budget_reached":
      return "/organisation/settings";
    default:
      return null;
  }
}

/** How a notification shows in the navigation's list: what the API read for it, where it opens. */
export function describeNotification(notification: Notification): NotificationView {
  return {
    heading: notification.title ?? HEADINGS[notification.kind] ?? "Notification",
    detail: notification.detail,
    to: notificationPath(notification),
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
