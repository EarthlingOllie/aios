import { describe, expect, it } from "vitest";

import { dataStoreNotice, dataStorePollInterval, dataStoreSummary } from "@/lib/data-store";

describe("dataStoreSummary", () => {
  it("says a ready store can be used", () => {
    expect(dataStoreSummary("ready")).toEqual({
      label: "Ready",
      message:
        "Your data store is ready. Your agents can keep data in it, and you can upload documents.",
    });
  });

  it("says a new store is being set up, and that uploads wait for it", () => {
    expect(dataStoreSummary("provisioning")).toEqual({
      label: "Being set up",
      message:
        "We're setting up your data store. This usually takes a few seconds; you can upload documents once it's ready.",
    });
  });

  it("says a store couldn't be set up and will be retried", () => {
    expect(dataStoreSummary("failed")).toEqual({
      label: "Failed",
      message: "Your data store couldn't be set up. We'll try again; please check back later.",
    });
  });

  it("says a store being deleted isn't available", () => {
    expect(dataStoreSummary("deleting")).toEqual({
      label: "Unavailable",
      message: "Your data store isn't available.",
    });
  });
});

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
