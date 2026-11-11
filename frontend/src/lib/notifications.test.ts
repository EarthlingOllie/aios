import { describe, expect, it } from "vitest";

import type { Notification } from "@/lib/api";
import { badgeText, describeNotification, notificationLink } from "@/lib/notifications";

function notification(fields: Partial<Notification>): Notification {
  return {
    id: "n1",
    organisation: { id: 7, slug: "acme" },
    kind: "schema_change_request",
    ref: { request_id: "r1", data_store_id: "s1" },
    title: "Create table data.contacts",
    created_at: "2026-10-17T09:00:00Z",
    read_at: null,
    ...fields,
  };
}

describe("describeNotification", () => {
  it("opens a schema change request on the Data page's Changes tab", () => {
    expect(describeNotification(notification({}))).toEqual({
      heading: "A schema change is waiting for you",
      detail: "Create table data.contacts",
      to: "/data/changes?request=r1",
      organisation: "acme",
    });
  });

  it("says when the request has gone", () => {
    expect(describeNotification(notification({ title: null })).detail).toBe(
      "This request no longer exists.",
    );
  });

  it("opens an organisation's scheduled deletion on its Organisation page's Settings tab", () => {
    const scheduled = notification({
      kind: "organisation_deletion_scheduled",
      ref: { organisation_name: "Acme", deletes_at: "2026-10-31T09:00:00Z" },
      title: "Acme is scheduled for deletion.",
    });
    expect(describeNotification(scheduled)).toEqual({
      heading: "Acme is scheduled for deletion.",
      detail: "Its creator can cancel it until then.",
      to: "/organisation/settings",
      organisation: "acme",
    });
  });

  it("opens a cancelled deletion on the Organisation page too", () => {
    const cancelled = notification({
      kind: "organisation_deletion_cancelled",
      ref: { organisation_name: "Acme" },
      title: "Acme will no longer be deleted.",
    });
    expect(describeNotification(cancelled)).toEqual({
      heading: "Acme will no longer be deleted.",
      detail: null,
      to: "/organisation",
      organisation: "acme",
    });
  });

  it("still shows a kind it doesn't know, with nowhere to go", () => {
    expect(describeNotification(notification({ kind: "store_over_quota", title: "Over quota" }))).toEqual({
      heading: "Over quota",
      detail: null,
      to: null,
      organisation: "acme",
    });
  });
});

describe("notificationLink", () => {
  const view = describeNotification(notification({}));

  it("opens one from the organisation the page is in within the page", () => {
    expect(notificationLink(view, "acme")).toEqual({ to: "/data/changes?request=r1", reload: false });
  });

  it("opens one from another organisation in that organisation, as a new page load", () => {
    expect(notificationLink(view, "globex")).toEqual({ to: "/o/acme/data/changes?request=r1", reload: true });
    expect(notificationLink(view, null)).toEqual({ to: "/o/acme/data/changes?request=r1", reload: true });
  });

  it("has nowhere to go for a kind the app doesn't know", () => {
    const unknown = describeNotification(notification({ kind: "store_over_quota" }));
    expect(notificationLink(unknown, "acme")).toBeNull();
  });
});

describe("badgeText", () => {
  it("is nothing with none unread", () => {
    expect(badgeText(0)).toBeNull();
  });

  it("counts up to nine, then 9+", () => {
    expect([1, 9, 10, 250].map(badgeText)).toEqual(["1", "9", "9+", "9+"]);
  });
});
