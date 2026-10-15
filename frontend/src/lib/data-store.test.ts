import { describe, expect, it } from "vitest";

import { dataStoreNotice, dataStorePollInterval } from "@/lib/data-store";

describe("dataStoreNotice", () => {
  it("says nothing about a ready store", () => {
    expect(dataStoreNotice("ready")).toBeNull();
  });

  it("says a new store is being set up", () => {
    expect(dataStoreNotice("provisioning")).toBe(
      "Setting up your data store. You can upload documents once it's ready.",
    );
  });

  it("says a store couldn't be set up", () => {
    expect(dataStoreNotice("failed")).toMatch(/^Your data store couldn't be set up/);
  });
});

describe("dataStorePollInterval", () => {
  it("stops polling once the store is ready", () => {
    expect(dataStorePollInterval("ready")).toBeNull();
  });

  it("polls a store being set up often, and a failed one rarely", () => {
    expect(dataStorePollInterval("provisioning")).toBe(2000);
    expect(dataStorePollInterval("failed")).toBe(15000);
  });
});
