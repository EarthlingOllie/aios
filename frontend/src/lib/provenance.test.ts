import { describe, expect, it } from "vitest";

import type { User } from "@/lib/api";
import { describeProvenance, describeStarter, personName } from "@/lib/provenance";

const me: User = { id: 1, login: "tester", name: "Tester", avatar_url: null };
const sam: User = { id: 2, login: "sam", name: "Sam Jones", avatar_url: null };
const alex: User = { id: 3, login: "alex", name: null, avatar_url: null };

describe("personName", () => {
  it("is the name, else the login, and 'you' for the signed-in user", () => {
    expect(personName(sam, me)).toBe("Sam Jones");
    expect(personName(alex, me)).toBe("alex");
    expect(personName(me, me)).toBe("you");
    expect(personName(me)).toBe("Tester");
  });
});

describe("describeProvenance", () => {
  it("says only who made it when they also changed it last", () => {
    expect(describeProvenance({ created_by: sam, updated_by: sam }, me)).toBe(
      "Created by Sam Jones",
    );
  });

  it("says who last changed it when that's someone else", () => {
    expect(describeProvenance({ created_by: sam, updated_by: alex }, me)).toBe(
      "Created by Sam Jones · last changed by alex",
    );
    expect(describeProvenance({ created_by: me, updated_by: sam }, me)).toBe(
      "Created by you · last changed by Sam Jones",
    );
  });

  it("says what's known when one of them isn't", () => {
    expect(describeProvenance({ created_by: null, updated_by: sam }, me)).toBe(
      "Last changed by Sam Jones",
    );
    expect(describeProvenance({ created_by: sam, updated_by: null }, me)).toBe(
      "Created by Sam Jones",
    );
    expect(describeProvenance({ created_by: null, updated_by: null }, me)).toBeNull();
  });
});

describe("describeStarter", () => {
  it("says which member started the run", () => {
    expect(describeStarter({ created_by: sam, trigger_id: null }, me)).toBe("Started by Sam Jones");
    expect(describeStarter({ created_by: me, trigger_id: null }, me)).toBe("Started by you");
  });

  it("says a schedule's run was scheduled", () => {
    expect(describeStarter({ created_by: null, trigger_id: 7 }, me)).toBe("Scheduled");
  });

  it("says nothing when who started it isn't known", () => {
    // From before runs recorded it, a deleted account, or a schedule since deleted.
    expect(describeStarter({ created_by: null, trigger_id: null }, me)).toBeNull();
  });
});
