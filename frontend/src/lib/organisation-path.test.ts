import { afterEach, describe, expect, it, vi } from "vitest";

import {
  inOrganisation,
  landingPath,
  organisationInPath,
  organisationToOpen,
  recentOrganisations,
  rememberOrganisation,
} from "@/lib/organisation-path";

const acme = { id: 1, name: "Acme", slug: "acme", is_creator: true, role: "admin" as const, deletes_at: null };
const globex = { id: 2, name: "Globex", slug: "globex", is_creator: false, role: "editor" as const, deletes_at: null };
const initech = { id: 3, name: "Initech", slug: "initech", is_creator: false, role: "editor" as const, deletes_at: null };

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
    expect(organisationToOpen([acme, globex], ["globex", "acme"])).toBe("globex");
  });

  it("opens the most recently used one the user is still in, once they've left the last", () => {
    expect(organisationToOpen([acme, globex, initech], ["umbrella", "initech", "globex"])).toBe("initech");
  });

  it("falls back to the one joined most recently (listed first) when none was used, or the user left them all", () => {
    expect(organisationToOpen([acme, globex], [])).toBe("acme");
    expect(organisationToOpen([acme, globex], ["initech"])).toBe("acme");
  });

  it("opens none when the user is in none", () => {
    expect(organisationToOpen([], ["acme"])).toBeNull();
  });
});

describe("landingPath", () => {
  it("lands in the most recently used organisation the user is still in", () => {
    expect(landingPath([globex, initech], ["acme", "initech"])).toBe("/o/initech/");
  });

  it("lands on the welcome page when the user is in none", () => {
    expect(landingPath([], ["acme"])).toBe("/welcome");
  });
});

describe("remembering the organisation used last", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps them in the browser, the one used most recently first", () => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => store.set(k, v),
    });
    expect(recentOrganisations()).toEqual([]);
    rememberOrganisation("globex");
    rememberOrganisation("acme");
    rememberOrganisation("globex");
    expect(recentOrganisations()).toEqual(["globex", "acme"]);
  });

  it("ignores what it can't read back", () => {
    const store = new Map<string, string>([["aios.organisations", "not json"]]);
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => store.set(k, v),
    });
    expect(recentOrganisations()).toEqual([]);
    store.set("aios.organisations", JSON.stringify({ not: "a list" }));
    expect(recentOrganisations()).toEqual([]);
    store.set("aios.organisations", JSON.stringify(["acme", 7, "globex"]));
    expect(recentOrganisations()).toEqual(["acme", "globex"]);
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
    expect(recentOrganisations()).toEqual([]);
  });
});
