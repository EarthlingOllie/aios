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
- When nothing matches: "No agents match “foo”." with a **Clear filters** button. This is
  separate from the existing empty message, which still shows when the list itself is empty.

The state lives in `MasterDetail` (`useState`), not the URL. It stays set while you move
between items, and resets when you leave the section. URL state (`?q=`) is the alternative
(see Open questions). It would also mean carrying the query string on every item `NavLink` and
on the `navigate("/agents")` calls after delete.

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
    shows the empty state, the filter is kept after opening an item.
  - Chats: the Joined / Not joined / Unread chips, and the filter still applies after a poll.
  - Workflows and Skills: one search case each.
- `pnpm typecheck`, `pnpm test`, `pnpm test:e2e`.

## Order of work

1. `lib/list-filter.ts` + unit tests.
2. `MasterDetail` search box, count and no-match state; turn it on for all four lists. This is
   usable on its own, and could ship first.
3. Filter chips and sort select; per-section configs.
4. E2E spec; a short line in the README under each section's description.

## Open questions

1. **URL or component state?** The plan keeps the query in component state. If you'd like
   filtered lists to be linkable or to survive a reload, it moves to `?q=&filter=&sort=`.
2. **Which filters matter to you?** The ones above are a guess at what's useful from the fields
   we already have. Filtering agents by a skill or tool, or skills by "used by an agent", needs
   more data in the list endpoints, so it would need backend work.
3. **Keyboard shortcut**: should `/` focus the list's search box? `/` isn't bound to anything
   today, but the chat composer would need to be excluded.
