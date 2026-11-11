import { afterEach, describe, expect, it, vi } from "vitest";

import {
  inOrganisation,
  lastOrganisation,
  organisationInPath,
  organisationToOpen,
  rememberOrganisation,
} from "@/lib/organisation-path";

const acme = { id: 1, name: "Acme", slug: "acme", is_creator: true };
const globex = { id: 2, name: "Globex", slug: "globex", is_creator: false };

describe("organisationInPath", () => {
  it("reads the slug from an organisation-prefixed path", () => {
    expect(organisationInPath("/o/acme/agents/1")).toBe("acme");
    expect(organisationInPath("/o/acme")).toBe("acme");
    expect(organisationInPath("/o/acme/")).toBe("acme");
  });

  it("finds none in a path without one", () => {
    expect(organisationInPath("/")).toBeNull();
    expect(organisationInPath("/agents/1")).toBeNull();
    expect(organisationInPath("/o/")).toBeNull();
    expect(organisationInPath("/organisation")).toBeNull();
  });
});

describe("inOrganisation", () => {
  it("puts a path without an organisation in the organisation's", () => {
    expect(inOrganisation("acme", "/agents/1?q=x#top")).toBe("/o/acme/agents/1?q=x#top");
    expect(inOrganisation("acme", "/")).toBe("/o/acme/");
    expect(inOrganisation("acme", "/?q=x")).toBe("/o/acme/?q=x");
  });

  it("leaves a path that already names an organisation as it is", () => {
    expect(inOrganisation("acme", "/o/globex/chats")).toBe("/o/globex/chats");
  });
});

describe("organisationToOpen", () => {
  it("opens the organisation used last", () => {
    expect(organisationToOpen([acme, globex], "globex")).toBe("globex");
  });

  it("falls back to the one joined most recently (listed first) when none was used, or the user left it", () => {
    expect(organisationToOpen([acme, globex], null)).toBe("acme");
    expect(organisationToOpen([acme, globex], "initech")).toBe("acme");
  });

  it("opens none when the user is in none", () => {
    expect(organisationToOpen([], "acme")).toBeNull();
  });
});

describe("remembering the organisation used last", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps it in the browser", () => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => store.set(k, v),
    });
    expect(lastOrganisation()).toBeNull();
    rememberOrganisation("globex");
    expect(lastOrganisation()).toBe("globex");
  });

  it("does without it where browser storage is refused", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
    });
    expect(() => rememberOrganisation("acme")).not.toThrow();
    expect(lastOrganisation()).toBeNull();
  });
});
