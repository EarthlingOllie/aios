# Search and filtering on the side lists

Status: **plan** (2026-10-19).

## Goal

Add a search box and a few filters to the lists on the left of the Agents, Workflows, Skills
and Chats sections, so a long list can be narrowed down quickly.

## What the codebase already gives us

- All four lists are drawn by one component, `MasterDetail` (`frontend/src/components/crud.tsx`).
  Each section passes its items, an empty message and a `renderItem`. Adding search there once
  gives all four lists the same behaviour.
- Each list endpoint (`/api/agents`, `/api/workflows`, `/api/skills`, `/api/chats`) returns
  every row in one response, and the page already holds that array. The lists are small, so
  filtering in the browser is enough. **No backend changes.**
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

## Design

### 1. A pure matching helper: `frontend/src/lib/list-filter.ts`

- `matchesQuery(fields: string[], query: string): boolean`. Splits the query on whitespace, and
  each term must appear in at least one field. Matching ignores case and accents (`NFD` plus
  stripping combining marks), so "cafe" finds "Café".
- `sortItems(items, sort)` for the shared sort orders: by name (`localeCompare`), and by most
  recently updated.
- Covered by `list-filter.test.ts` (vitest), the same as the other `lib/*.test.ts` files.

### 2. `MasterDetail` gains optional props

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

### 3. Per-section configuration

- **Agents**: search name and description. Filters: All · Used (has runs) · Unused. Sorts:
  Name · Recently updated · Most runs.
- **Workflows**: search name and description. Filters: All · Empty (no nodes). Sorts: Name ·
  Recently updated.
- **Skills**: search name and description. No filters (the summary doesn't say which agents use
  a skill). Sorts: Name · Recently updated.
- **Chats**: search name and description. Filters: All · Joined · Not joined · Unread ·
  Private. No sort control: the list stays in the server's order, most recently active first.

## Tests

- **Unit** (`list-filter.test.ts`): multi-term matching, case and accents, an empty query
  matches everything, both sort orders.
- **E2E** (Playwright, against `e2e/fake-api.ts`), as `e2e/list-search.spec.ts`:
  - Agents: typing narrows the list, the count shows, clearing restores it, a no-match query
    shows the empty state, the filter is kept after opening an item, `?q=` in the URL is
    applied on load and Back restores the previous filter.
  - The `/` shortcut focuses the search box, and still types `/` inside the chat composer.
  - Chats: the Joined / Not joined / Unread chips, and the filter still applies after a poll.
  - Workflows and Skills: one search case each.
- `pnpm typecheck`, `pnpm test`, `pnpm test:e2e`.

## Order of work

1. `lib/list-filter.ts` + unit tests.
2. `MasterDetail` search box, count and no-match state; turn it on for all four lists. This is
   usable on its own, and could ship first.
3. Filter chips and sort select; per-section configs.
4. E2E spec; a short line in the README under each section's description.
5. (Optional) the extra filters below.

## Optional: filters that need backend data

Both are small, because the queries they need mostly exist already:

- **Agents by skill or tool.** `GET /api/agents` already joins each agent's current version to
  count its skills and tools (`skill_count`, `tool_count` in `api/agents.py`). It would also
  return the ids: `skill_ids: number[]` and `tool_keys: string[]`. The Agents list then gets two
  selects, "Any skill" and "Any tool", filled from `listSkills` and `listTools`
  (`?skill=12&tool=web_search`).
- **Skills used or unused.** `api/skills.py` already has the query "a live agent whose current
  version uses some version of this skill", which refuses a delete. Reused as a count, it gives
  `SkillSummary.agent_count`, and the Skills list gets chips: All · Used · Unused.

The work: two list endpoints and their response models, backend tests for the new fields, the
same fields in `e2e/fake-api.ts` and the TypeScript types, and the chips and selects (which
reuse step 3's machinery). Roughly 250–350 lines including tests, about half of it tests. That
is about the size of steps 1–2. It's independent of the rest, so it can go in the same PR or
just after.


## Decisions

- Filters, search and sort live in the URL query (answered 2026-10-19).
- The filters in step 3 are agreed.
- `/` focuses the list's search box.
