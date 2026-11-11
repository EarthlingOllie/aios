import { describe, expect, it } from "vitest";

import { type Organisation, type User } from "@/lib/api";
import { describeOrganisation, leaveRefusal, linkName } from "@/lib/organisation";

const me: User = { id: 1, name: "Terry Tester" };
const sam: User = { id: 2, name: "Sam Jones" };
const base: Organisation = {
  id: 1,
  name: "Terry Tester",
  slug: "terry-tester",
  personal: true,
  is_creator: true,
  creator: me,
  members: [],
  created_at: "2026-10-14T08:00:00Z",
};

describe("describeOrganisation", () => {
  it("calls the user's own personal organisation theirs", () => {
    expect(describeOrganisation(base, me)).toBe("Your personal organisation, created by you.");
  });

  it("names the creator of someone else's personal organisation", () => {
    const org = { ...base, is_creator: false, creator: sam };
    expect(describeOrganisation(org, me)).toBe("A personal organisation, created by Sam Jones.");
  });

  it("names the creator of an organisation that isn't personal", () => {
    const org = { ...base, personal: false, is_creator: false, creator: sam };
    expect(describeOrganisation(org, me)).toBe("Created by Sam Jones.");
  });

  it("says when the creator's account is gone", () => {
    expect(describeOrganisation({ ...base, personal: false, creator: null }, me)).toBe(
      "Its creator's account has been deleted.",
    );
    expect(describeOrganisation({ ...base, creator: null }, me)).toBe(
      "Your personal organisation.",
    );
  });
});

describe("leaveRefusal", () => {
  const member = (user: User) => ({ user, joined_at: "2026-10-14T08:00:00Z", invite_id: null });

  it("tells the last member to delete the organisation instead", () => {
    expect(leaveRefusal({ ...base, members: [member(me)] })).toBe(
      "You're its only member, so nobody would be left to see what's in it. Delete the organisation instead.",
    );
  });

  it("refuses the creator while others remain", () => {
    expect(leaveRefusal({ ...base, personal: false, members: [member(me), member(sam)] })).toBe(
      "You created it, and only its creator can invite people, so you can't leave while others are still members.",
    );
  });

  it("lets any other member leave", () => {
    const org = { ...base, is_creator: false, creator: sam, members: [member(sam), member(me)] };
    expect(leaveRefusal(org)).toBeNull();
  });
});

describe("linkName", () => {
  it("names a link by the start of its id, the same wherever it's shown", () => {
    expect(linkName("3f2a9c1e-0000-4000-8000-000000000000")).toBe("link 3f2a9c1e");
  });
});
