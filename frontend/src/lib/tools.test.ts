import { describe, expect, it } from "vitest";

import type { ConnectorTool } from "@/lib/api";
import { connectorToolOptions, pinConnectorTools, workflowOptions } from "@/lib/tools";

const live = [
  { id: 1, name: "Summarise" },
  { id: 2, name: "Report" },
];
const archived = [{ id: 3, name: "Old" }];

describe("workflowOptions", () => {
  it("lists the workflows that can be picked, those picked first", () => {
    expect(workflowOptions(live, archived, [2])).toEqual([
      { id: 2, name: "Report", checked: true, note: null },
      { id: 1, name: "Summarise", checked: false, note: null },
    ]);
  });

  it("keeps picked workflows since archived or deleted, saying so, so they can be removed", () => {
    expect(workflowOptions(live, archived, [3, 9, 1])).toEqual([
      { id: 1, name: "Summarise", checked: true, note: null },
      { id: 3, name: "Old", checked: true, note: "archived" },
      { id: 9, name: "Workflow 9", checked: true, note: "deleted" },
      { id: 2, name: "Report", checked: false, note: null },
    ]);
  });

  it("doesn't offer archived workflows that aren't picked", () => {
    expect(workflowOptions(live, archived, []).map((o) => o.id)).toEqual([1, 2]);
  });
});

const tool = (name: string, read_only = false): ConnectorTool => ({
  name,
  description: `${name} things.`,
  input_schema: {},
  read_only,
});

describe("connectorToolOptions", () => {
  it("lists the connector's tools, those pinned first", () => {
    expect(connectorToolOptions([tool("post"), tool("read", true)], ["read"])).toEqual([
      { name: "read", description: "read things.", readOnly: true, checked: true, gone: false },
      { name: "post", description: "post things.", readOnly: false, checked: false, gone: false },
    ]);
  });

  it("keeps pinned tools the connector no longer offers, saying so, so they can be removed", () => {
    expect(connectorToolOptions([tool("post")], ["vanished"])).toEqual([
      { name: "vanished", description: "", readOnly: false, checked: true, gone: true },
      { name: "post", description: "post things.", readOnly: false, checked: false, gone: false },
    ]);
  });

  it("lists only the pinned tools while the connector's can't be listed", () => {
    expect(connectorToolOptions(undefined, ["post"])).toEqual([
      { name: "post", description: "", readOnly: false, checked: true, gone: false },
    ]);
  });
});

describe("pinConnectorTools", () => {
  const test = { id: 1, provider: "test" };
  const slack = { id: 2, provider: "slack" };

  it("pins a connector's tools, adding its group", () => {
    expect(pinConnectorTools([], test, ["post"])).toEqual([
      { connector_id: 1, provider: "test", tools: ["post"] },
    ]);
  });

  it("replaces a group's tools and leaves the others", () => {
    const pins = [
      { connector_id: 1, provider: "test", tools: ["post"] },
      { connector_id: 2, provider: "slack", tools: ["send"] },
    ];
    expect(pinConnectorTools(pins, test, ["post", "read"])).toEqual([
      { connector_id: 1, provider: "test", tools: ["post", "read"] },
      { connector_id: 2, provider: "slack", tools: ["send"] },
    ]);
  });

  it("drops a group left with no tools", () => {
    const pins = [{ connector_id: 2, provider: "slack", tools: ["send"] }];
    expect(pinConnectorTools(pins, slack, [])).toEqual([]);
  });
});
