import { describe, expect, it } from "vitest";

import type { Notification } from "@/lib/api";
import { badgeText, describeNotification, notificationLink } from "@/lib/notifications";

function notification(fields: Partial<Notification>): Notification {
  return {
    id: "n1",
    organisation: { id: 7, slug: "acme", name: "Acme" },
    kind: "approval_needed",
    ref: { approval_id: "r1" },
    title: "A schema change is waiting for you",
    detail: "Create table data.contacts",
    created_at: "2026-10-17T09:00:00Z",
    read_at: null,
    ...fields,
  };
}

describe("describeNotification", () => {
  it("opens an approval on the Data page's Changes tab, saying what the API read for it", () => {
    expect(describeNotification(notification({}))).toEqual({
      heading: "A schema change is waiting for you",
      detail: "Create table data.contacts",
      to: "/data/approvals?approval=r1",
      organisation: "acme",
    });
  });

  it("opens a mention, a reply or a chat's failed run in its chat", () => {
    for (const kind of ["mention", "reply", "run_failed"]) {
      const view = describeNotification(
        notification({
          kind,
          ref: { chat_id: 4, message_id: 12 },
          title: "Ada mentioned you in Design",
          detail: "@You look",
        }),
      );
      expect(view).toEqual({
        heading: "Ada mentioned you in Design",
        detail: "@You look",
        to: "/chats/4",
        organisation: "acme",
      });
    }
  });

  it("opens a failed run started by hand on its run page", () => {
    expect(describeNotification(notification({ kind: "run_failed", ref: { agent_id: 3, run_id: 9 } })).to).toBe(
      "/agents/3/runs/9",
    );
    expect(
      describeNotification(
        notification({
          kind: "run_failed",
          ref: { workflow_id: 5, run_id: 2 },
        }),
      ).to,
    ).toBe("/workflows/5/runs/2");
  });

  it("opens a released document that changed on the Data page's Documents tab", () => {
    expect(
      describeNotification(
        notification({
          kind: "released_document_changed",
          ref: { document_id: 4 },
        }),
      ).to,
    ).toBe("/data/documents");
  });

  it("opens a source that needs attention on its page, and a changed released item on the flagged list", () => {
    const attention = describeNotification(notification({ kind: "source_attention", ref: { source_id: 7 }, title: null }));
    expect(attention.to).toBe("/data/sources/7");
    expect(attention.heading).toBe("A source needs attention");
    expect(describeNotification(notification({ kind: "released_item_changed", ref: { synced_item_id: 3 } })).to).toBe(
      "/organisation/settings",
    );
  });

  it("opens an organisation's deletion on its Organisation page", () => {
    expect(describeNotification(notification({ kind: "organisation_deletion_scheduled", ref: {} })).to).toBe(
      "/organisation/settings",
    );
    expect(describeNotification(notification({ kind: "organisation_deletion_cancelled", ref: {} })).to).toBe(
      "/organisation",
    );
  });

  it("opens a budget's warning and its being reached on the organisation's settings", () => {
    for (const kind of ["budget_warning", "budget_reached"]) {
      const view = describeNotification(notification({ kind, ref: { month: "2026-11" }, title: null }));
      expect(view.to).toBe("/organisation/settings");
      expect(view.heading).toMatch(/monthly budget/);
    }
  });

  it("still says what kind it is while its organisation's store can't be read", () => {
    const unread = notification({
      kind: "reply",
      ref: { chat_id: 4, message_id: 12 },
      title: null,
      detail: null,
    });
    expect(describeNotification(unread).heading).toBe("A reply in a chat");
  });

  it("still shows a kind it doesn't know, with nowhere to go", () => {
    expect(
      describeNotification(
        notification({
          kind: "store_over_quota",
          title: "Over quota",
          detail: null,
        }),
      ),
    ).toEqual({
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
    expect(notificationLink(view, "acme")).toEqual({
      to: "/data/approvals?approval=r1",
      reload: false,
    });
  });

  it("opens one from another organisation in that organisation, as a new page load", () => {
    expect(notificationLink(view, "globex")).toEqual({
      to: "/o/acme/data/approvals?approval=r1",
      reload: true,
    });
    expect(notificationLink(view, null)).toEqual({
      to: "/o/acme/data/approvals?approval=r1",
      reload: true,
    });
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
