# 07: Notifications across organisations: mentions, replies, failures

**What to build:** The bell covers every organisation a person belongs to, each notification labelled with its organisation, and updates live from the person stream. Its ten-second poll goes. (Notifications carry their organisation from Phase 0 · 10.)

- **Routes.** Notifications move to person-scoped routes, and the old routes and reference keys go in the same change.
- **Rendering.** Each notification's text is built when it's read, by resolving its reference in that organisation's store. A notification never stores content.
- **New kinds:**
  - **mention:** a person mentioned by another person;
  - **reply:** whoever mentioned an agent or workflow, when its result is posted, unless they're watching the conversation in a visible tab;
  - **run failed:** whoever caused a failed run started by hand or by mention.
- **Approvals.** Today's schema change request notifications become **approval needed**.
- **Read marking and cleanup.**
  - At most one unread notification per thing.
  - Reading a conversation past a message clears the notifications about it.
  - **Mark all read** clears everything.
  - Leaving an organisation removes its notifications.
  - Old read notifications are pruned.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [04: Streamed conversations and chat list, with limits and fallback](04-streaming.md)

**Status:** ready-for-agent

- [ ] The bell lists notifications from every organisation the person is in, each labelled with its organisation. Clicking one from another organisation switches to it and opens the notification.
- [ ] New and read notifications reach the bell through the person stream, with no polling.
- [ ] A notification whose reference no longer exists (pruned, or in an organisation the person left) is dropped from the list and marked read.
- [ ] Mention notifications go to the people mentioned, never for mentioning yourself.
- [ ] The reply notification is skipped while the asker has the conversation's stream open in a visible tab; the client reports visibility on the stream.
- [ ] A failed run notifies whoever caused it.
- [ ] Repeats about the same thing never create a second unread notification.
- [ ] Reading a conversation past a message clears the reply, mention and run-failed notifications about it. Mark all read works.
- [ ] Approval notifications behave as before under the "approval needed" kind, and clear for everyone once the approval is decided, cancelled or expired.
- [ ] Leaving an organisation deletes its notifications for that person. Read notifications older than the retention setting are pruned.
- [ ] The old notification routes and reference keys are gone, and the browser tests use only the new routes.
