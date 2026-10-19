import { describe, expect, it } from "vitest";

import type { DataStoreUsage } from "@/lib/api";
import {
  dataStoreNotice,
  dataStorePollInterval,
  dataStoreSummary,
  storageMeters,
  uploadsRefused,
} from "@/lib/data-store";

describe("dataStoreSummary", () => {
  it("says a ready store can be used", () => {
    expect(dataStoreSummary("ready")).toEqual({
      label: "Ready",
      message:
        "The organisation's data store is ready. Its agents can keep data in it, and you can upload documents.",
    });
  });

  it("says a new store is being set up, and that uploads wait for it", () => {
    expect(dataStoreSummary("provisioning")).toEqual({
      label: "Being set up",
      message:
        "We're setting up the organisation's data store. This usually takes a few seconds; you can upload documents once it's ready.",
    });
  });

  it("says a store couldn't be set up and will be retried", () => {
    expect(dataStoreSummary("failed")).toEqual({
      label: "Failed",
      message: "The organisation's data store couldn't be set up. We'll try again; please check back later.",
    });
  });

  it("says a store being deleted isn't available", () => {
    expect(dataStoreSummary("deleting")).toEqual({
      label: "Unavailable",
      message: "The organisation's data store isn't available.",
    });
  });

  it("says the same as the notice about a store that failed or isn't available", () => {
    for (const status of ["failed", "deleting"] as const) {
      expect(dataStoreSummary(status).message).toBe(dataStoreNotice(status, "search"));
    }
  });
});

describe("dataStoreNotice", () => {
  it("says nothing about a ready store", () => {
    expect(dataStoreNotice("ready", "search")).toBeNull();
  });

  it("says a new store is being set up, and what can be done once it's ready", () => {
    expect(dataStoreNotice("provisioning", "upload documents")).toBe(
      "Setting up the organisation's data store. You can upload documents once it's ready.",
    );
    expect(dataStoreNotice("provisioning", "search")).toBe(
      "Setting up the organisation's data store. You can search once it's ready.",
    );
  });

  it("says a store couldn't be set up", () => {
    expect(dataStoreNotice("failed", "search")).toMatch(/^The organisation's data store couldn't be set up/);
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

const usage = (over: Partial<DataStoreUsage> = {}): DataStoreUsage => ({
  db_bytes: 12 * 1024 ** 2,
  db_quota_bytes: 1024 ** 3,
  files_bytes: 300 * 1024 ** 2,
  files_quota_bytes: 5 * 1024 ** 3,
  measured_at: "2026-10-17T09:00:00Z",
  ...over,
});

describe("storageMeters", () => {
  it("gives each cap's usage, as a share, with no warning well under it", () => {
    expect(storageMeters(usage())).toEqual([
      {
        id: "db",
        label: "Data stored",
        used: "12 MB of 1.0 GB",
        share: 12 / 1024,
        level: "ok",
        warning: null,
      },
      {
        id: "files",
        label: "Documents",
        used: "300 MB of 5.0 GB",
        share: 300 / (5 * 1024),
        level: "ok",
        warning: null,
      },
    ]);
  });

  it("warns from 80% of a cap of what happens at it", () => {
    const [db, files] = storageMeters(
      usage({ db_bytes: 0.8 * 1024 ** 3, files_bytes: 4.5 * 1024 ** 3 }),
    );
    expect([db.level, files.level]).toEqual(["near", "near"]);
    expect(db.warning).toBe(
      "Your data is nearly at its cap. Once it is, your agents can't add or change rows, and documents can't be uploaded.",
    );
    expect(files.warning).toBe(
      "Your documents are nearly at their cap. Once they are, uploads are refused, and your agents can't add or change rows.",
    );
  });

  it("says what's refused at a cap, and what still works", () => {
    const [db, files] = storageMeters(usage({ db_bytes: 1024 ** 3, files_bytes: 6 * 1024 ** 3 }));
    expect([db.level, files.level]).toEqual(["full", "full"]);
    expect(db.warning).toBe(
      "Your data is at its cap: your agents can't add or change rows, and documents can't be uploaded. Reading still works, and deleting rows makes room.",
    );
    expect(files.warning).toBe(
      "Your documents are at their cap: uploads are refused, and your agents can't add or change rows. Delete some to make room.",
    );
    // A share past the cap is drawn as a full bar.
    expect(files.share).toBe(1);
  });

  it("says usage isn't known until it's first measured", () => {
    const [db, files] = storageMeters(
      usage({ db_bytes: null, files_bytes: null, measured_at: null }),
    );
    expect([db.used, db.level, db.share, db.warning]).toEqual([
      "Not measured yet",
      "unknown",
      0,
      null,
    ]);
    expect(files.used).toBe("Not measured yet");
  });
});

describe("uploadsRefused", () => {
  it("is true once either cap is reached, as the API refuses uploads at either", () => {
    expect(uploadsRefused(usage())).toBe(false);
    expect(uploadsRefused(usage({ files_bytes: 5 * 1024 ** 3 }))).toBe(true);
    expect(uploadsRefused(usage({ db_bytes: 1024 ** 3 }))).toBe(true);
    expect(uploadsRefused(usage({ db_bytes: null, files_bytes: null }))).toBe(false);
    expect(uploadsRefused(undefined)).toBe(false);
  });
});
