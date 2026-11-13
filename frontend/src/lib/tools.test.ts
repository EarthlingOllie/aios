import { describe, expect, it } from "vitest";

import type { ConnectorTool } from "@/lib/api";
import {
  connectorToolOptions,
  parseTargets,
  pinConnectorTargets,
  pinConnectorTools,
  targetsLabel,
  workflowOptions,
} from "@/lib/tools";

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

  it("sets a limited tool's allowed channels", () => {
    const pins = [{ connector_id: 2, provider: "slack", tools: ["post_to_slack"] }];
    expect(pinConnectorTools(pins, slack, ["post_to_slack"], { post_to_slack: ["C1", "C2"] })).toEqual([
      { connector_id: 2, provider: "slack", tools: ["post_to_slack"], allowed: { post_to_slack: ["C1", "C2"] } },
    ]);
  });

  it("keeps the allowed channels when the tools change, but not for a tool unpinned", () => {
    const pins = [
      { connector_id: 2, provider: "slack", tools: ["post_to_slack"], allowed: { post_to_slack: ["C1"] } },
    ];
    expect(pinConnectorTools(pins, slack, ["post_to_slack", "read"])).toEqual([
      { connector_id: 2, provider: "slack", tools: ["post_to_slack", "read"], allowed: { post_to_slack: ["C1"] } },
    ]);
    expect(pinConnectorTools(pins, slack, ["read"])).toEqual([
      { connector_id: 2, provider: "slack", tools: ["read"] },
    ]);
  });
});

describe("allowed targets", () => {
  const github = { id: 3, provider: "github" };

  it("keeps a group's allowed targets when its tools change", () => {
    const pins = [{ connector_id: 3, provider: "github", tools: ["open_issue"], targets: ["acme/api"] }];
    expect(pinConnectorTools(pins, github, ["open_issue", "comment_on_issue"])).toEqual([
      { connector_id: 3, provider: "github", tools: ["open_issue", "comment_on_issue"], targets: ["acme/api"] },
    ]);
  });

  it("sets a pinned group's allowed targets, and leaves unpinned connectors alone", () => {
    const pins = [{ connector_id: 3, provider: "github", tools: ["open_issue"] }];
    expect(pinConnectorTargets(pins, 3, ["acme/api", "acme/web"])).toEqual([
      { connector_id: 3, provider: "github", tools: ["open_issue"], targets: ["acme/api", "acme/web"] },
    ]);
    expect(pinConnectorTargets(pins, 9, ["acme/api"])).toEqual(pins);
  });

  it("reads targets typed with commas, spaces or new lines, once each", () => {
    expect(parseTargets(" acme/api, acme/web\nacme/api  ")).toEqual(["acme/api", "acme/web"]);
    expect(parseTargets("  ")).toEqual([]);
  });

  it("names what a provider's tools act on", () => {
    expect(targetsLabel("github")).toBe("Allowed repositories");
    expect(targetsLabel("test")).toBe("Allowed targets");
  });
});
