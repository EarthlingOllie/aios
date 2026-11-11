import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import Markdown from "react-markdown";
import { describe, expect, it } from "vitest";

import type { ChatMention } from "./api";
import {
  composerMentionables,
  filterMentionable,
  insertMention,
  mentionAt,
  mentionQuery,
  mentionsIn,
  mentionToken,
  remarkMentions,
  type Mentionable,
  type Mentionables,
} from "./mentions";

const user = (id: number, first_name: string, last_name: string): Mentionable => ({
  kind: "user",
  id,
  name: `${first_name} ${last_name}`,
  first_name,
  last_name,
});
const target = (kind: "agent" | "workflow", id: number, name: string): Mentionable => ({
  kind,
  id,
  name,
  first_name: null,
  last_name: null,
});
const alice = user(1, "Alice", "Smith");
const sam = user(2, "Sam", "Jones");
const reviewAgent = target("agent", 7, "Review");
const reviewWorkflow = target("workflow", 7, "Review");
const digest = target("workflow", 9, "Digest");
const members: Mentionables = { users: [alice, sam], agents: [reviewAgent], workflows: [reviewWorkflow, digest] };

describe("mentionToken", () => {
  it("writes the member's name, kind and id", () => {
    expect(mentionToken(reviewAgent)).toBe("@[Review](agent:7)");
    expect(mentionToken(reviewWorkflow)).toBe("@[Review](workflow:7)");
  });

  it("escapes what would end the name early", () => {
    expect(mentionToken({ ...digest, name: "a]b\\c[d" })).toBe("@[a\\]b\\\\c\\[d](workflow:9)");
  });
});

describe("mentionAt", () => {
  it("reads the kind and id a mention's link points at", () => {
    expect(mentionAt("agent:7")).toEqual({ kind: "agent", id: 7 });
    expect(mentionAt("user:12")).toEqual({ kind: "user", id: 12 });
  });

  it("is null for any other link", () => {
    for (const href of ["https://example.com", "agent:", "agent:7x", "robot:7", "agent:-1", "#agent:7"]) {
      expect(mentionAt(href)).toBeNull();
    }
  });
});

describe("mentionsIn", () => {
  it("is the mentionable members a body's tokens name, each once, in order", () => {
    const body = `${mentionToken(reviewWorkflow)} then ${mentionToken(reviewAgent)}, ${mentionToken(reviewWorkflow)}`;
    expect(mentionsIn(body, members)).toEqual([
      { kind: "workflow", id: 7 },
      { kind: "agent", id: 7 },
    ]);
  });

  it("reads names with escapes", () => {
    const odd = { ...digest, name: "a]b\\c" };
    expect(mentionsIn(`hi ${mentionToken(odd)}!`, members)).toEqual([{ kind: "workflow", id: 9 }]);
  });

  it("leaves out tokens for anyone who isn't mentionable, and plain @names", () => {
    expect(mentionsIn("@[Ghost](agent:99) and @Review and [Review](agent:7)", members)).toEqual([]);
  });
});

describe("mentionQuery", () => {
  it("is the @ word being typed at the caret", () => {
    expect(mentionQuery("Hi @rev", 7)).toEqual({ start: 3, query: "rev" });
    expect(mentionQuery("@", 1)).toEqual({ start: 0, query: "" });
    expect(mentionQuery("one\n@Sa more", 7)).toEqual({ start: 4, query: "Sa" });
  });

  it("is null when the caret isn't in an @ word", () => {
    expect(mentionQuery("Hi there", 8)).toBeNull();
    expect(mentionQuery("Hi @rev done", 12)).toBeNull();
    // An email address isn't a mention.
    expect(mentionQuery("me@example", 10)).toBeNull();
    // Nor is an inserted mention.
    expect(mentionQuery("@[Review](agent:7)", 18)).toBeNull();
  });
});

describe("insertMention", () => {
  it("replaces the @ word with the member's token and a space, and puts the caret after it", () => {
    const text = "Hi @rev, thanks";
    const { text: next, caret } = insertMention(text, { start: 3, query: "rev" }, reviewAgent);
    expect(next).toBe("Hi @[Review](agent:7) , thanks");
    expect(next.slice(0, caret)).toBe("Hi @[Review](agent:7) ");
  });
});

describe("filterMentionable", () => {
  const onlyUsers = (...users: Mentionable[]) => ({ users, agents: [], workflows: [] });

  it("keeps users whose first name has the query", () => {
    expect(filterMentionable(members, "ali")).toEqual(onlyUsers(alice));
    expect(filterMentionable(members, "ice")).toEqual(onlyUsers(alice));
  });

  it("keeps users whose last name has the query", () => {
    expect(filterMentionable(members, "smi")).toEqual(onlyUsers(alice));
    expect(filterMentionable(members, "ones")).toEqual(onlyUsers(sam));
  });

  it("ignores case", () => {
    expect(filterMentionable(members, "ALI")).toEqual(onlyUsers(alice));
    expect(filterMentionable(members, "sMiTh")).toEqual(onlyUsers(alice));
  });

  it("doesn't match a user across their first and last names", () => {
    expect(filterMentionable(members, "cesmi")).toEqual(onlyUsers());
  });

  it("keeps agents and workflows whose name has the query, ignoring case, grouped by kind", () => {
    expect(filterMentionable(members, "rev")).toEqual({ users: [], agents: [reviewAgent], workflows: [reviewWorkflow] });
    expect(filterMentionable(members, "GES")).toEqual({ users: [], agents: [], workflows: [digest] });
  });

  it("keeps everyone for an empty query", () => {
    expect(filterMentionable(members, "")).toEqual(members);
  });
});

describe("remarkMentions", () => {
  const render = (body: string, mentions: ChatMention[]) =>
    renderToStaticMarkup(createElement(Markdown, { remarkPlugins: [remarkMentions(mentions)] }, body));

  it("marks each mention the message makes with its kind, id and stored name", () => {
    const mentions: ChatMention[] = [
      { kind: "agent", id: 7, name: "Review" },
      { kind: "workflow", id: 7, name: "Review (old name)" },
    ];
    expect(render("Ask @[Review](agent:7), then **@[Review](workflow:7)**.", mentions)).toBe(
      '<p>Ask <span data-mention="agent:7">Review</span>, then ' +
        '<strong><span data-mention="workflow:7">Review (old name)</span></strong>.</p>',
    );
  });

  it("reads escaped names", () => {
    const odd = { kind: "workflow", id: 9, name: "a]b" } as const;
    expect(render(`hi ${mentionToken({ ...odd, first_name: null, last_name: null })}`, [odd])).toBe(
      '<p>hi <span data-mention="workflow:9">a]b</span></p>',
    );
  });

  it("leaves a token for someone the message doesn't mention as plain text", () => {
    expect(render("ping @[Outsider](agent:3)", [])).toBe("<p>ping @Outsider</p>");
  });

  it("leaves ordinary links, and kind:id links without an @, alone", () => {
    expect(render("[docs](https://example.com) and [Review](agent:7)", [{ kind: "agent", id: 7, name: "Review" }])).toBe(
      '<p><a href="https://example.com">docs</a> and <a href="">Review</a></p>',
    );
  });
});

describe("composerMentionables", () => {
  const person = { kind: "user" as const, id: 1, name: "Ann Lee", first_name: "Ann", last_name: "Lee" };
  const included = { kind: "agent" as const, id: 2, name: "Bot", first_name: null, last_name: null };

  it("adds the organisation's agents and workflows the chat doesn't include, after its own", () => {
    const chat = { users: [person], agents: [included], workflows: [] };
    expect(
      composerMentionables(chat, [
        { kind: "agent", id: 2, name: "Bot" },
        { kind: "agent", id: 3, name: "Scout" },
        { kind: "workflow", id: 2, name: "Digest" },
      ]),
    ).toEqual({
      users: [person],
      agents: [included, { kind: "agent", id: 3, name: "Scout", first_name: null, last_name: null }],
      workflows: [{ kind: "workflow", id: 2, name: "Digest", first_name: null, last_name: null }],
    });
  });

  it("is the chat's own until the organisation's have loaded", () => {
    const chat = { users: [person], agents: [included], workflows: [] };
    expect(composerMentionables(chat, undefined)).toBe(chat);
    expect(composerMentionables(undefined, [])).toBeUndefined();
  });
});
