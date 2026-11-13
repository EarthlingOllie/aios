# 06: GitHub

**What to build:** Organisations can connect GitHub. Admins install the aios GitHub App on chosen repositories and pick from them in aios. Issues, pull requests and their comments sync as message items with state, labels, author and link; optionally, README and docs Markdown on the default branch sync as documents. Agents can comment on issues and pull requests and open issues, only in allowed repositories, signed with the agent's name and audited.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [04: Connectors: credentials, allowlist, choosing what syncs, removal](04-connectors.md), [05: Connector tools and the MCP client](05-connector-tools-and-mcp.md)

**Status:** done

- [x] Only the installation id is stored; installation tokens are minted when needed.
- [x] Recorded responses cover updates since the cursor, comments (including review comments), transfers, deletions and a repository removed from scope; reconciliation removes transferred or deleted issues.
- [x] Turning docs syncing on brings in README and docs Markdown as documents, a changed file is re-ingested, a deleted one removed, and turning it off removes them.
- [x] Repositories added to the installation later aren't synced until picked in aios.
- [x] A comment or issue in a repository outside the tool's allowed list is refused and the model is told.
- [x] An allowed comment or issue is posted as the App, signed "— <agent name> via aios", and written to the audit log.
- [x] The connector shows its health on the Data page.

**Notes:** connecting checks the installation with the admin's own GitHub authorisation (the App
asks for it during installation), so two more settings are needed beside the App id, private key
and slug: `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_CLIENT_SECRET`. GitHub's tools are served by aios
itself (REST, as the App) rather than through an MCP server; the allowed repositories are each
connector pin's `targets`. Recorded responses: `backend/tests/recordings/github.json`, replayed by
`backend/tests/fake_github.py`.
