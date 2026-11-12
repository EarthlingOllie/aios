import { describe, expect, it } from "vitest";

import type { Connector, ScopeItem } from "@/lib/api";
import {
  connectorState,
  RESTRICTED_WARNING,
  restrictedPicked,
  scopeChanged,
} from "@/lib/connectors";

const general: ScopeItem = {
  id: "general",
  name: "general",
  restricted: false,
};
const board: ScopeItem = { id: "board", name: "board", restricted: true };
const connector = (fields: Partial<Connector>) =>
  ({
    id: 1,
    provider: "test",
    name: "Test",
    status: "active",
    unavailable: null,
    scope: [],
    ...fields,
  }) as Connector;

describe("restrictedPicked", () => {
  it("is what's picked and restricted at its source", () => {
    const known = [general, board];
    expect(restrictedPicked(known, ["general"])).toEqual([]);
    expect(restrictedPicked(known, ["general", "board"])).toEqual([board]);
  });
});

describe("scopeChanged", () => {
  it("tells a different pick from the saved one, whatever the order", () => {
    expect(scopeChanged([general, board], ["board", "general"])).toBe(false);
    expect(scopeChanged([general], ["general", "board"])).toBe(true);
    expect(scopeChanged([general, board], ["board"])).toBe(true);
  });
});

describe("connectorState", () => {
  it("says what needs doing, unavailability first", () => {
    expect(connectorState(connector({}))).toEqual({
      label: "Connected",
      attention: false,
    });
    expect(connectorState(connector({ status: "needs_reauth" }))).toEqual({
      label: "Reconnect",
      attention: true,
    });
    expect(connectorState(connector({ status: "removing" }))).toEqual({
      label: "Being removed",
      attention: false,
    });
    expect(
      connectorState(
        connector({
          status: "needs_reauth",
          unavailable: "Test is switched off.",
        }),
      ),
    ).toEqual({
      label: "Unavailable",
      attention: true,
    });
  });
});

it("warns that everyone in the organisation will be able to search restricted items", () => {
  expect(RESTRICTED_WARNING).toContain(
    "everyone in the organisation will be able to search it",
  );
});
