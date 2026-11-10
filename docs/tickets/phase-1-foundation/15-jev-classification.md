# 15: Jev: classify untrusted content

**What to build:** aios checks untrusted content with Jev, the existing prompt-injection classifier used by laya-php's System One package, before it can reach a model. Jev judges prompt injection only, not other kinds of unsafe content. aios doesn't build a classifier: a Python client implements a `classify(content, source) -> verdict` interface over Jev's HTTP API, with its URL and key in `.env` (Jev as a container in Compose or hosted, as for laya-php). One verdict set is used everywhere: `awaiting_check`, `clean`, `flagged`, `released` and `derived`.

- **Documents** are classified at ingestion, with the verdict stored on the document and copied onto its search items. A flagged document is kept but left out of search and context. Uploaders see each document's verdict status.
- **Tool results** are classified inline when the tool returns, for every tool except a trusted list of internal tools whose output doesn't come from outside: `describe_data`'s schema listing, `search` (its items carry stored verdicts), and the platform's own lookups (chat members, agent and workflow listings, approval status). That covers web search, web fetch, `query_data`, and `run_python` output with sandbox files read back. A flagged result is blocked and the agent sees an error instead.
- **`query_data`** classifies text cells in chunks of up to 4,000 characters, the chunks of one result in parallel within the timeout. Flagged cells are redacted to "[withheld: flagged by Jev]" rather than blocking the result. Rows from tables with a stored verdict of `clean`, `released` or `derived` aren't classified again; the text of `flagged` and `awaiting_check` rows is redacted.
- **Derived text** that agents write from possibly untrusted content (run messages, briefs, and later phases' posts, handbacks, deliveries, node outputs, compaction summaries and recalled memory) isn't classified again. It gets the verdict `derived`: searchable, and always wrapped. Until phase 2 checks chat messages at posting, people's chat messages are indexed as `derived` too.
- **Wrapping.** Every untrusted input and all derived text is wrapped in an untrusted block wherever it enters a model's context, whatever its verdict, in case Jev misclassifies.
- **Fails closed.** A document Jev can't check stays `awaiting_check`, isn't searchable, is retried by ingestion, and is re-checked by a periodic sweep. A tool result that can't be checked within the timeout is replaced by an error the agent sees. A health check feeds an alert and metrics, and people see a notice while Jev is down.
- **Flagged list.** Admins see flagged documents with Jev's reason on the Settings tab and can release one. A released document is searchable and reaches context, but is still wrapped as untrusted.
- **Re-checking.** When Jev's model or policy version changes, a job re-classifies stored content. A released item whose content changes goes back to `awaiting_check`, and the admin who released it is notified.
- `make seed` runs with the fake Jev. Sending content to Jev, possibly hosted, is an accepted risk for the demo.
- Phase 2 adds the check on chat messages at posting, phase 3 on `remember()` content and trigger webhook bodies, and phase 4 on synced source items and connector and MCP results, all through this client.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [06: Unified search across documents, chats and runs](06-unified-search.md), [08: Audit log and approval rules](08-audit-log-and-approval-rules.md)

**Status:** ready-for-agent

- [ ] Jev's OpenAPI document is fetched first, and the client follows it.
- [ ] A fake implementing the same `classify` interface serves the unit tests and `make seed`; one contract test runs the client against real Jev.
- [ ] Documents and search items carry a verdict column with the values `awaiting_check`, `clean`, `flagged`, `released` and `derived`; a document's search items copy its verdict, and run messages and briefs are indexed as `derived`.
- [ ] Ingestion classifies each document before its chunks become searchable; a flagged document is stored with its verdict and left out of search and agents' context.
- [ ] The Documents page shows each document's verdict status to its uploader.
- [ ] With Jev unavailable, a new document stays `awaiting_check` and isn't searchable; once Jev is back, ingestion's retry or the awaiting-check sweep classifies it.
- [ ] Web search, web fetch and `run_python` results (with sandbox files read back) are classified inline; a flagged result reaches the agent as an error, and so does one Jev couldn't check in time.
- [ ] Tools on the trusted list (`describe_data`'s schema listing, `search`, the platform's own lookups) aren't sent to Jev.
- [ ] `query_data` classifies text cells in parallel chunks, redacts flagged cells to "[withheld: flagged by Jev]", redacts the text of `flagged` and `awaiting_check` rows, and doesn't re-classify rows already `clean`, `released` or `derived`.
- [ ] Every tool result, every search result and all derived text reaches model context inside an untrusted block, whatever its verdict.
- [ ] Search returns only items whose verdict is `clean`, `released` or `derived`.
- [ ] A health check polls Jev and raises an alert and metrics when it's down, and people see a notice in the app meanwhile.
- [ ] The Settings tab shows admins a flagged list with Jev's reason; releasing a document makes it searchable, keeps it wrapped as untrusted, and is recorded in the audit log.
- [ ] After Jev's version changes, the re-check job re-classifies stored content; a released document whose content changed goes back to `awaiting_check` and its releasing admin is notified.
- [ ] Editors and participants can't see the flagged list or release anything (403).
