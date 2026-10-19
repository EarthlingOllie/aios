import { describe, expect, it } from "vitest";

import type { Organisation, User } from "@/lib/api";
import { describeOrganisation } from "@/lib/organisation";

const me: User = { id: 1, login: "tester", name: "Tester", avatar_url: null };
const sam: User = { id: 2, login: "sam", name: "Sam Jones", avatar_url: null };
const base: Organisation = {
  id: 1,
  name: "tester",
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

  it("falls back to the creator's login when they have no name", () => {
    const org = { ...base, personal: false, is_creator: false, creator: { ...sam, name: null } };
    expect(describeOrganisation(org, me)).toBe("Created by sam.");
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
