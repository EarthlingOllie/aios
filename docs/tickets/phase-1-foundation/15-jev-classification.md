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

**Status:** done, but for the two boxes left open below (the hosted OpenAPI document couldn't be fetched from the build sandbox, and web search runs inside the provider's response)

- [ ] Jev's OpenAPI document is fetched first, and the client follows it.
  (Not ticked: TypeSafe's document (`https://api.typesafe.ai/openapi.json`) couldn't be fetched
  from the build sandbox, whose egress proxy refuses that host. The client follows the same
  wire contract as documented by laya-serve (`docs/http-api.md`, "Strict Jev contract", generated
  from its FastAPI app) and by laya-php's contract test against TypeSafe's document: `POST
  /v1/systemone` with `state`, `questions` and `model`, a bearer key, one `noul` question
  answered as `answers.<id>.noul`, and `GET /health`. `aios.jev`'s docstring records it. Fetch
  the document and run the contract test against hosted Jev before relying on it.)
- [x] A fake implementing the same `classify` interface serves the unit tests and `make seed`; one contract test runs the client against real Jev.
  (`aios.jev`: `JevClient` and `FakeJev` share `classify(content, source) -> Judgement`,
  `healthy()` and `version()`; `JEV_URL` picks one (`fake`, a URL, or empty: nothing can be
  checked). conftest's autouse `jev_classifier` installs the fake; `make seed` runs under
  `jev.use(FakeJev())` and seeds a clean and a flagged document. `test_jev.py`'s
  `test_contract_against_real_jev` runs when `JEV_CONTRACT_URL` (and `JEV_CONTRACT_KEY`) name
  one; Compose's `jev` profile runs laya-serve for it.)
- [x] Documents and search items carry a verdict column with the values `awaiting_check`, `clean`, `flagged`, `released` and `derived`; a document's search items copy its verdict, and run messages and briefs are indexed as `derived`.
  (Store revision 0016: `aios.verdict`, `documents.verdict` (plus reason, version, checked_at,
  content hash and who released it), `search_items.verdict`, existing non-chunk items backfilled
  `derived`. `aios.search_index` writes chat messages, run messages and briefs as `derived`.
  `test_verdicts.py`, `test_provisioning.py`'s metadata comparison.)
- [x] Ingestion classifies each document before its chunks become searchable; a flagged document is stored with its verdict and left out of search and agents' context.
  (Ingest indexes chunks `awaiting_check`, then `aios.verdicts.check_document` classifies them in
  parallel batches and stores the verdict on the document and its items.
  `test_a_clean_document_is_checked_at_ingestion_and_found`,
  `test_a_flagged_document_is_kept_with_its_reason_but_never_found`.)
- [x] The Documents page shows each document's verdict status to its uploader.
  (`DocumentOut.verdict`/`verdict_reason`; the Data page's Documents and the chat panel show a
  badge and what it means (`lib/verdicts.ts`). `test_the_documents_list_shows_...`,
  `e2e/jev.spec.ts`.)
- [x] With Jev unavailable, a new document stays `awaiting_check` and isn't searchable; once Jev is back, ingestion's retry or the awaiting-check sweep classifies it.
  (Ingest raises `JevUnavailable` after storing it; `ingest_document_task` queues
  `aios.check_document` (every minute, ten times); the beat's `check-stored-content` sweep
  (`JEV_SWEEP_INTERVAL_SECONDS`) checks whatever still waits. `test_with_jev_down_...`,
  `test_ingestion_retries_the_check_...`, `test_the_sweep_runs_for_every_ready_store`.)
- [ ] Web search, web fetch and `run_python` results (with sandbox files read back) are classified inline; a flagged result reaches the agent as an error, and so does one Jev couldn't check in time.
  (Partly: `aios.tool_checks.ToolResultChecks`, middleware on every agent, classifies every
  result but the trusted list's inline, `run_python`'s included (its output is all it returns:
  there's no separate read-back of sandbox files), and a flagged or unchecked one becomes an
  error. Not ticked because web search is Anthropic's server-side tool: its results enter the
  model's context inside the provider's own response, so nothing can check them first; and
  there's no web fetch tool. Both need a client-side search/fetch tool, a product decision.
  `test_a_tool_result_is_checked_...`, `test_a_flagged_tool_result_...`,
  `test_a_tool_result_jev_cant_check_in_time_...`.)
- [x] Tools on the trusted list (`describe_data`'s schema listing, `search`, the platform's own lookups) aren't sent to Jev.
  (`tool_checks.TRUSTED`: `describe_data`, `search`, `search_documents`, `search_history`,
  `write_data` and `request_schema_change` (outcomes and approval status). Phase 1 has no
  member or listing tools yet. `test_trusted_tools_arent_sent_to_jev_but_are_still_wrapped`.)
- [x] `query_data` classifies text cells in parallel chunks, redacts flagged cells to "[withheld: flagged by Jev]", redacts the text of `flagged` and `awaiting_check` rows, and doesn't re-classify rows already `clean`, `released` or `derived`.
  (`aios.tools.data._screen`: cells packed into chunks of at most 4,000 characters, classified
  at once; a flagged pack's cells are checked one by one. Text read from the platform's indexed
  tables takes its search item's stored verdict, found by MD5 (indexed in 0016); awaiting rows
  read "[withheld: awaiting Jev's check]". Jev down: the result is an error.
  `test_query_data_*` in `test_untrusted.py`.)
- [x] Every tool result, every search result and all derived text reaches model context inside an untrusted block, whatever its verdict.
  (Every tool message, search tools' included, is wrapped (`jev.wrap`, which escapes a closing
  tag inside); a run's brief is wrapped in its input, and the handoff's transcript of the chat
  is; agents' and the handoff's system prompts say what the block means (`UNTRUSTED_RULE`). The
  mentioning message itself stays plain: it's the request the agent is asked to act on. Run pages
  show tool results out of their block (`ui_stream`). `test_untrusted.py`, `test_chat_handoff.py`.)
- [x] Search returns only items whose verdict is `clean`, `released` or `derived`.
  (`search.Filters.sql` always filters by verdict: the tools and the Search page.
  `test_search_finds_clean_released_and_derived_items_only`.)
- [x] A health check polls Jev and raises an alert and metrics when it's down, and people see a notice in the app meanwhile.
  (`aios.jev_health.poll` on the beat (`check-jev-health`): a `JevUp` CloudWatch embedded
  metric each poll, an ERROR `ALERT` log line once per outage, and the state in Redis for `GET
  /api/jev-status`; the shell shows a banner while it's down (`JevNotice`). Terraform's
  `jev.tf` alarms on the metric; not applied here (deploy-only). `test_jev.py`, `e2e/jev.spec.ts`.)
- [x] The Settings tab shows admins a flagged list with Jev's reason; releasing a document makes it searchable, keeps it wrapped as untrusted, and is recorded in the audit log.
  (`GET /api/o/{org}/flagged`, `POST .../flagged/{id}/release` (`document.released`), the
  Settings tab's Flagged content section. `test_admins_see_the_flagged_list_...`,
  `test_releasing_a_document_...`, `e2e/jev.spec.ts`.)
- [x] After Jev's version changes, the re-check job re-classifies stored content; a released document whose content changed goes back to `awaiting_check` and its releasing admin is notified.
  (A verdict's version is Jev's model (with laya-serve's checkpoint revisions where `/health`
  reports them) and our policy hash; the sweep re-checks clean and flagged documents judged under
  another, and undoes a release whose content hash changed (notification
  `released_document_changed`). `test_a_new_jev_version_rechecks_...`,
  `test_a_released_document_*`.)
- [x] Editors and participants can't see the flagged list or release anything (403).
  (`Action.release`, admins only. `test_editors_and_participants_cant_...`,
  `test_permissions.py`, `e2e/jev.spec.ts`.)
