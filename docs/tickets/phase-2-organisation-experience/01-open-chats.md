# 01: Open chats, and the Jev check on posted messages

**What to build:** Every conversation is an open chat. Anyone in the organisation can read any chat without joining, and posting joins you to it. Each chat includes the agents and workflows chosen for it, and only those can be mentioned there. Every message a person posts is checked by Jev before it's stored.

- **Reading.** The chat list shows every chat in the organisation, joined or not. Anyone in the organisation can read a chat's messages, chat members and run cards.
- **Posting joins.** Posting in a chat you haven't joined joins you, in the same transaction as the message.
- **Included agents and workflows.** The agents and workflows among a chat's chat members are the ones it includes. A message mentioning one the chat doesn't include is refused, naming it, and the screen offers **Add it to this chat**, which adds it and sends the message.
- **The Jev check.** In the posting path, after the permission and mention checks and before anything is written, the message goes through phase 1's `classify(content, source)` with the source "chat message". Jev flags prompt injection only. (Ticket 02 adds the archived check, ahead of this one.)
  - A flagged message is refused with the reason and nothing is written: no message, invocation, search item, notification or stream event. The screen keeps the text in the composer.
  - If Jev errors or doesn't answer within the timeout (a few seconds), the message is refused with "couldn't check this message, try again", and nothing is written. While phase 1's Jev health check reports Jev down, the composer shows a notice saying so.
  - A message that passes is stored with the verdict `clean`, copied onto its search item.
  - People's messages are wrapped in an untrusted block wherever they enter model context (the mentioning message, the chat history the handoff reads, and the asking message given to handback in ticket 08), whatever the verdict. Briefs and run messages are derived text and are wrapped too.
  - Posts by agents and workflows aren't checked here; tickets 08 and 09 store them as `derived`.
- **Old columns go.** The chats' visibility and the chat members' role are dropped in this change, with their enum types. Nothing is migrated: dev data is rebuilt with `make seed`.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [Phase 1 · 05: Triggers, chats, invocations and schema change requests move](../phase-1-foundation/05-move-triggers-chats-approvals.md), [Phase 1 · 10: An invocation for every run, and turns as records](../phase-1-foundation/10-invocations-and-turns.md), [Phase 1 · 15: Jev: classify untrusted content](../phase-1-foundation/15-jev-classification.md)

**Status:** done

Owner-only actions became any chat member's here, since the role they hung on is dropped
(renaming, describing, adding and removing chat members, deleting); ticket 02 narrows them to its
rules (nobody removes a person, no delete, archiving). Leaving the organisation no longer hands
chats over, for the same reason.

- [x] A member who hasn't joined a chat can list it and read its messages, chat members and run cards.
  (`GET /chats` lists every chat with `is_member`; reads need no membership (`get_visible_chat`).
  `test_open_chats.py::test_a_member_who_hasnt_joined_lists_and_reads_any_chat`,
  `test_chat_joining.py`, `e2e/chat-joining.spec.ts`.)
- [x] Posting in a chat you haven't joined joins you, and the chat then counts unread for you.
  (`post_message` joins the poster in the message's transaction (`members.join`, insert on
  conflict do nothing, under the chat's lock), with what was there counted as read.
  `test_posting_in_a_chat_you_havent_joined_joins_you`, `e2e/chat-joining.spec.ts`.)
- [x] A message mentioning an agent or workflow the chat doesn't include gets a 422 naming it, and nothing is stored. **Add it to this chat** adds it and sends the message.
  (`resolve_mentions`: `detail: {message, missing: [{kind, id, name}]}`; the composer offers the
  organisation's other agents and workflows after the chat's (`composerMentionables`), and the
  room's refusal has **Add it to this chat** (joins first if need be, adds each, resends).
  `test_mentioning_an_agent_the_chat_doesnt_include_is_refused_naming_it`,
  `e2e/open-chats.spec.ts`.)
- [x] With Jev's fake flagging a message, the post gets a 422 with the reason, and nothing is stored, indexed, notified or streamed.
  (`check_with_jev` after the permission and mention checks, before any write: `detail:
  {message, reason}`. Nothing posts notifications or streams yet; no message, membership,
  invocation or search item is written. `test_a_message_jev_flags_is_refused_...`.)
- [x] With Jev's fake failing or slower than the timeout, the post gets a 503 saying to try again, and nothing is stored.
  (`JEV_CHAT_TIMEOUT_SECONDS`, 3 by default. `test_a_message_jev_cant_check_in_time_...[down]`,
  `[slow]`.)
- [x] A message that passes carries the verdict `clean`, and so does its search item.
  (Store revision 0019 adds `chat_messages.verdict` (`derived` by default, for agents' and
  workflows' posts); `index_chat_message` copies it.
  `test_a_message_that_passes_is_stored_clean_and_so_is_its_search_item`.)
- [x] With the scripted model, a mentioned agent receives people's messages and briefs inside untrusted blocks.
  (`invocations.run_input` wraps the message (source "chat message") as well as the brief; the
  handoff wraps the mentioning message as well as the chat before it.
  `test_a_mentioned_agent_gets_peoples_messages_and_the_brief_in_untrusted_blocks`,
  `test_untrusted.py`, `test_chat_handoff.py`.)
- [x] While the Jev health check reports Jev down, the composer shows the notice.
  (`ChatRoom` reads `GET /jev-status`; `composerJevNotice`. `verdicts.test.ts`,
  `e2e/open-chats.spec.ts`.)
- [x] Two chats can't share a name (409).
  (`test_names_are_unique_among_the_organisations_chats`,
  `test_chat_names_are_unique_within_the_organisation`.)
- [x] No code reads visibility or the chat member role, and the columns and their types are gone.
  (Store revision 0019 drops `chats.visibility`, `chat_members.role`, their check and one-owner
  index, and the `chat_visibility` and `chat_member_role` types; `ChatVisibility` and
  `ChatMemberRole` are gone, and so are the screens' visibility field, Private filter and owner
  badge. `test_provisioning.py`'s model/migration comparison.)
- [x] Browser tests cover reading without joining, **Add it to this chat**, a refused message kept in the composer, and the Jev-down notice.
  (`e2e/chat-joining.spec.ts`, `e2e/open-chats.spec.ts`.)
