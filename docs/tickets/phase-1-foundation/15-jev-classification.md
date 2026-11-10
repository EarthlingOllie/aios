# 15: Jev: classify untrusted content

**What to build:** aios checks untrusted content with Jev, the existing prompt-injection and content classifier used by laya-php's System One package, before it can reach a model. aios doesn't build a classifier: a Python client implements a `classify(content, source) -> verdict` interface over Jev's HTTP API, with its URL and key in `.env` (Jev as a container in Compose or hosted, as for laya-php). Documents are classified at ingestion, with the verdict stored on the document and copied onto its search items; a flagged document is kept but left out of search and context. Every tool result from outside (web search, web fetch, `query_data`) is classified inline when the tool returns; a flagged one is blocked and the agent sees an error instead. Every untrusted input is also wrapped in an untrusted block in model context, whatever its verdict, in case Jev misclassifies. Admins see a flagged list on the Settings tab and can release a flagged document, marking it trusted.

- Fails closed: a document Jev can't check stays "awaiting check", is retried by ingestion and isn't searchable until it has a verdict; a tool result that can't be checked within the timeout is replaced by an error the agent sees.
- A released document is searchable and reaches context, but is still wrapped as untrusted.
- Phase 2 adds the check on chat messages at posting, and phase 4 the check on synced source items, both through this client.

[Spec](../../specs/phase-1-foundation.md)

**Blocked by:** [06: Unified search across documents, chats and runs](06-unified-search.md), [08: Audit log and approval rules](08-audit-log-and-approval-rules.md)

**Status:** ready-for-agent

- [ ] Jev's OpenAPI document is fetched first, and the client follows it.
- [ ] A fake implementing the same `classify` interface serves the unit tests; one contract test runs the client against real Jev.
- [ ] Documents and search items carry a verdict column: awaiting check, clean, flagged or released; a document's search items copy its verdict.
- [ ] Ingestion classifies each document before its chunks become searchable; a flagged document is stored with its verdict and left out of search and agents' context.
- [ ] With Jev unavailable, a new document stays "awaiting check", isn't searchable, and is classified when ingestion retries.
- [ ] Web search, web fetch and `query_data` results are classified inline; a flagged result reaches the agent as an error, and so does one Jev couldn't check in time.
- [ ] Every tool result from outside, and every search result, reaches model context inside an untrusted block, whatever its verdict.
- [ ] The Settings tab shows admins a flagged list with Jev's reason; releasing a document makes it searchable, keeps it wrapped as untrusted, and is recorded in the audit log.
- [ ] Editors and participants can't see the flagged list or release anything (403).
