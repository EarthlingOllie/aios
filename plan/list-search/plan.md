# Search and filtering on the side lists, and tabs on the detail pages

Status: **plan** (2026-10-19).

## Goal

1. Add a search box and filters to the lists on the left of the Agents, Workflows, Skills and
   Chats sections, so a long list can be narrowed down quickly (Part 1).
2. Split the workflow, skill and chat detail pages into tabs, as the agent and Data pages
   already are (Part 2).

## What the codebase already gives us

- All four lists are drawn by one component, `MasterDetail` (`frontend/src/components/crud.tsx`).
  Each section passes its items, an empty message and a `renderItem`. Adding search there once
  gives all four lists the same behaviour.
- Each list endpoint (`/api/agents`, `/api/workflows`, `/api/skills`, `/api/chats`) returns
  every row in one response, and the page already holds that array. The lists are small, so
  filtering in the browser is enough. Only the agent and skill filters that need data the
  lists don't send yet touch the backend (see 3b).
- `MasterDetail` is the layout route, so it stays mounted while you click between items. State
  kept in it survives opening an item. The Chats list polls every 5s and replaces `items`, and
  a filter applied on each render keeps working across those reloads.
- Each summary type already has the fields the filters need:

| List | Search fields | Fields for filters and sorting |
| --- | --- | --- |
| Agents (`AgentSummary`) | name, description | `run_count`, `skill_count`, `tool_count`, `updated_at` |
| Workflows (`WorkflowSummary`) | name, description | `node_count`, `updated_at` |
| Skills (`SkillSummary`) | name, description | `updated_at` |
| Chats (`ChatListed`) | name, description | `is_member`, `visibility`, `unread_count`, `updated_at` |

## Part 1: search and filters

### Design

#### 1. A pure matching helper: `frontend/src/lib/list-filter.ts`

- `matchesQuery(fields: string[], query: string): boolean`. Splits the query on whitespace, and
  each term must appear in at least one field. Matching ignores case and accents (`NFD` plus
  stripping combining marks), so "cafe" finds "Café".
- `sortItems(items, sort)` for the shared sort orders: by name (`localeCompare`), and by most
  recently updated.
- Covered by `list-filter.test.ts` (vitest), the same as the other `lib/*.test.ts` files.

#### 2. `MasterDetail` gains optional props

```ts
search?: (item: T) => string[];            // the fields the search box matches
filters?: { key: string; label: string; test: (item: T) => boolean }[];  // chips, "All" first
sorts?: { key: string; label: string; compare: (a: T, b: T) => number }[];
```

If a section passes none of these, the list renders exactly as it does today. Under the header
it adds:

- A search `Input` with a search icon, `aria-label="Search <title>"`, and a clear (×) button
  when it has text. Escape also clears it.
- A row of filter chips (one active at a time; "All" is the default) when `filters` is given.
- A small sort `<select>` (using `selectClass`) when `sorts` is given. The default is the
  server's order.
- When something is filtered out, a "3 of 12" count, so it's clear the list is narrowed.
- Pressing `/` anywhere in the section focuses the search box, unless you're already typing in
  an input, textarea, select or contenteditable (so `/` still types in the chat composer and
  the editors). A small `/` hint shows in the box.
- When nothing matches: "No agents match “foo”." with a **Clear filters** button. This is
  separate from the existing empty message, which still shows when the list itself is empty.

The state lives in the URL, as `?q=&filter=&sort=` (`useSearchParams`, as the Data tabs and
Search page already do), so a filtered list can be linked to, survives a reload, and works with
Back. Defaults are left out of the URL, so `/agents` stays `/agents`. Typing replaces the
history entry rather than pushing one per keystroke. To keep the filter while you move around
the section:

- Each item's `NavLink` (and the **New** button) carries the current query string.
- The section's own `navigate("/agents")` calls (after a delete, for example) go through a
  small `useListPath()` helper that adds the query string back on.

The selected item stays open on the right even when the filter hides it in the list.

#### 3. Per-section configuration

- **Agents**: search name and description. Filters: All · Used (has runs) · Unused. Sorts:
  Name · Recently updated · Most runs. Plus the skill and tool selects in 3b.
- **Workflows**: search name and description. Filters: All · Empty (no nodes). Sorts: Name ·
  Recently updated.
- **Skills**: search name and description. Filters: All · Used · Unused (3b). Sorts: Name ·
  Recently updated.
- **Chats**: search name and description. Filters: All · Joined · Not joined · Unread ·
  Private. No sort control: the list stays in the server's order, most recently active first.

#### 3b. Filters that need backend data

Both are small, because the queries they need mostly exist already:

- **Agents by skill or tool.** `GET /api/agents` already joins each agent's current version to
  count its skills and tools (`skill_count`, `tool_count` in `api/agents.py`). It also returns
  the ids: `skill_ids: list[int]` and `tool_keys: list[str]`. The Agents list gets two selects,
  "Any skill" and "Any tool", filled from `listSkills` and `listTools`
  (`?skill=12&tool=web_search`). A skill or tool in the URL that no longer exists falls back
  to "Any".
- **Skills used or unused.** `api/skills.py` already has the query "a live agent whose current
  version uses some version of this skill", which refuses a delete. Reused as a count, it gives
  `SkillSummary.agent_count`, and the Skills list gets chips: All · Used · Unused.

`MasterDetail` takes these as extra `filters` groups, so a list can have chips and selects
together. Each group has its own URL parameter, and they combine with AND.

Backend tests: extend `test_agents.py` for `skill_ids`, `tool_keys` (current version only, not
older versions), and `test_skills.py` (or the existing skill tests) for `agent_count`, which
must ignore deleted agents and agents whose *current* version dropped the skill. Add the same
fields to `e2e/fake-api.ts` and the types in `lib/api.ts`.

### Tests

- **Unit** (`list-filter.test.ts`): multi-term matching, case and accents, an empty query
  matches everything, both sort orders, combining filter groups.
- **Backend** (pytest): the new summary fields, as in 3b.
- **E2E** (Playwright, against `e2e/fake-api.ts`), as `e2e/list-search.spec.ts`:
  - Agents: typing narrows the list, the count shows, clearing restores it, a no-match query
    shows the empty state, the filter is kept after opening an item, `?q=` in the URL is
    applied on load and Back restores the previous filter. The skill and tool selects.
  - The `/` shortcut focuses the search box, and still types `/` inside the chat composer.
  - Chats: the Joined / Not joined / Unread chips, and the filter still applies after a poll.
  - Skills: search, and the Used / Unused chips. Workflows: one search case.

## Part 2: tabs on the detail pages

### What we already have

`Tabs` (`components/tabs.tsx`, helpers in `lib/tabs.ts`) keeps the open tab in `?tab=`. The
first tab is the default and leaves the URL bare, and switching replaces the history entry. It
follows the ARIA tabs pattern and shows an optional count beside each label. Only the open
panel is mounted. The agent page uses it (Context · Runs · Versions · Schedule), and so does
the Data page. Other pages link straight to a tab, e.g. `/agents/12?tab=runs` from a run page.

### Tabs per page

Things that apply to the whole item stay above the tabs: the title, actions (Edit, Delete,
Run, Join/Leave), error banners, and the workflow's inline "start a run" form.

| Page | Tabs (first is the default) | Moves from |
| --- | --- | --- |
| Workflow (`WorkflowDetailView`) | **Graph** · **Runs** (count) · **Versions** (count) · **Schedule** | The Graph, Runs, Version history and Schedules sections, which are stacked today. Same ids as the agent page: `runs`, `versions`, `schedule`. |
| Skill (`SkillDetail`) | **Content** · **Used by** (count) · **Versions** (count) | "When to use" + "Instructions" become Content; "Used by" and "Version history" get their own tabs. |
| Chat (`ChatDetail`) | **Chat** · **Members** (count) | The chat room, and the Members section under it. |

### Links that should open a tab

Today these go back to the bare detail page. They should open the tab they came from:

- `WorkflowRun.tsx`: the back link and the post-delete `navigate` → `/workflows/{id}?tab=runs`.
- `Workflows.tsx` (`WorkflowVersionView`): the back link and the post-restore `navigate` →
  `?tab=versions`.
- `Skills.tsx` (`SkillVersionView`): the same → `/skills/{id}?tab=versions`.
- `AgentDeleteDialog.tsx`: a workflow's schedules link → `/workflows/{id}?tab=schedule`, as
  the agent's own schedules link already does.

### Things to watch

- **The chat room unmounts on the Members tab.** Only the open panel is mounted, so switching
  to Members and back reloads the messages and resets the scroll position. Unread marking
  pauses while you're on Members, which is correct, since you aren't reading. The unsent draft
  in the composer would be lost, so it moves up into `ChatDetail` state (keyed by chat id), or
  `sessionStorage`, so it survives a tab switch.
- **The graph remounts** when you come back to the Graph tab, so the node selection resets. That
  matches how the agent page's tabs behave and is fine.
- **Tabs and list filters share the query string.** `Tabs` already keeps other parameters when
  it switches, so `?q=…&tab=runs` works. The list's item links carry only the list's own
  parameters (`q`, `filter`, `sort`, `skill`, `tool`), not `tab`, so opening another item
  starts on its first tab.

### Tests

- New e2e specs modelled on `e2e/agent-tabs.spec.ts`: `workflow-tabs.spec.ts`,
  `skill-tabs.spec.ts`, `chat-tabs.spec.ts`. Each checks the default tab and a bare URL,
  switching updates `?tab=`, counts, a deep link opens the right tab, and the back links above.
  The chat spec also checks that a draft survives a switch to Members and back.
- Existing specs that expect every section on one page need a tab click first, probably
  `workflows-view`, `workflow-runs`, `schedules`, `chats` and `chat-joining` (to be confirmed
  by running the suite).

## Order of work

1. `lib/list-filter.ts` + unit tests.
2. `MasterDetail` search box, URL state, count, no-match state and the `/` shortcut; turn it on
   for all four lists. This is usable on its own, and could ship first.
3. Filter chips and sort select; per-section configs.
4. Backend fields for 3b, with their tests; the skill, tool and Used / Unused filters.
5. E2E spec for the lists; a short line in the README under each section's description.
6. Tabs: workflow page, then skill page, then chat page (with the draft fix), each with its
   e2e spec and the back-link fixes. Update the README where it describes these pages.

Parts 1 and 2 touch different code (the list component vs the detail pages), so they can be
separate PRs. Suggested split: PR 1 is steps 1–5, PR 2 is step 6.

## Decisions

- Filters, search and sort live in the URL query (answered 2026-10-19).
- The filters in step 3 are agreed, and the backend-backed filters (3b) are in scope.
- `/` focuses the list's search box.
- The workflow, skill and chat detail pages get tabs (Part 2).
