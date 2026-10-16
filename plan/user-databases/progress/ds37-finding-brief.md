# Brief for #37 review-finding agents (frontend)

You investigate ONE code-review finding on branch `claude/data-stores-37-data-page` (head 1bdd9e0cefa915973e1da434e494e444eb40b3b0) of EarthlingOllie/aios. The branch builds issue #37 "Data stores 4b: Data page with overview and documents". It is frontend-only:
- `frontend/src/pages/Data.tsx`: the page and its tabs.
- `frontend/src/components/Documents.tsx`: the shared hooks `useDataStoreStatus` and `useDocuments`, the chat sidebar panel, and `DocumentsSection`.
- `frontend/src/lib/data-store.ts`: `dataStoreNotice` and `dataStoreSummary`.
- `frontend/e2e/fake-api.ts`, and `frontend/e2e/data-page.spec.ts` (Playwright against the in-memory fake API).

Read the issue with the GitHub MCP tools (ToolSearch `select:mcp__github__issue_read`, owner EarthlingOllie, repo aios, issue 37) if you need context.

## Setup
- `git fetch origin claude/data-stores-37-data-page && git checkout -b claude/ds37-f<N> 1bdd9e0cefa915973e1da434e494e444eb40b3b0` (N = your finding number). Commit there. Do NOT push, and don't touch any other branch: the orchestrator cherry-picks from your local branch.
- Commit messages end with:
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01KC3fucdE9rANvvSzGLn7e5
- In `frontend/`: `pnpm install --frozen-lockfile`, then `pnpm typecheck && pnpm test && pnpm build`. For e2e: `E2E_PORT=<5700+N> PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome pnpm exec playwright test`. Never run `playwright install`.
- You don't need Postgres or the backend unless your finding needs real-app behaviour. If it does, copy the harness from /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds37/drive/ and the environment steps from /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/builder-brief.md. Use your own ports: Postgres 5700+N, Redis 6700+N, API 8700+N. Clean up afterwards.
- Other agents are fixing other findings in the same files at the same time. Keep your diff minimal and focused, so cherry-picks merge.

## Process
1. Investigate: trace a realistic path from a real user action to the failure. Decide REAL, NOT REAL or PARTLY. If the fix costs more than it buys, say so and build nothing.
2. If real: invoke the Skill tool with skill `mattpocock-skills:tdd`. Write a failing test first (a vitest unit test, or a Playwright e2e test against the fake API that reproduces the user-visible failure) and save its red output. Then make the minimal fix in the style of the surrounding code, and save the green output.
3. Run typecheck, vitest, build and the full e2e suite, and save the outputs. Take Playwright screenshots of the before and after where the change is visible.
4. Save evidence to /tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/ds37/f<N>/.
5. Return: the verdict with the traced path, what you changed and why, the commit sha(s) on `claude/ds37-f<N>`, test counts, and absolute paths to the evidence files.
