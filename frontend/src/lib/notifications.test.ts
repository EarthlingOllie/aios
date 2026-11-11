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
