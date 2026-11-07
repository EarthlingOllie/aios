# 02: Chats without owners, and archiving chats

**What to build:** Chats stop having owners, and all chat members are equal.

- **Any chat member** can:
  - rename a chat and change its description;
  - add people;
  - add or remove agents and workflows.
- **Only admins** remove people from a chat. Someone removed can rejoin, because chats are open.
- **Anyone** can leave.
- **Leaving the organisation** takes a person out of every conversation, with nothing handed over.
- **Archiving.** Admins can archive and unarchive chats. An archived chat is read-only, with a banner, but still found by search. Runs already under way in it finish and post their results.
- **No delete.** Deleting a chat goes away.
- **Screens follow permissions.** The permission check returns the actions a person may take, and the screens show or hide them accordingly.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [01: Visibility check for conversations; private chats become direct messages](01-visibility-and-direct-message-migration.md), [Phase 1 · 08: Audit log and approval rules](../phase-1-foundation/08-audit-log-and-approval-rules.md)

**Status:** ready-for-agent

- [ ] A table-driven test through the API covers every chat action in the spec's permission table, for each role (participant, editor, admin), both in and out of the chat.
- [ ] A former owner can leave a chat. Leaving the organisation hands nothing over.
- [ ] A chat member trying to remove another person gets a 403. An admin can remove a person, who can then rejoin.
- [ ] Archive and unarchive are admin-only. Archiving a direct message is a 404.
- [ ] An archived chat refuses posts, mentions, joins, edits and adds with a 409 and a clear message. Its messages still appear in search, and it's listed under an Archived filter.
- [ ] Runs already under way in a chat when it's archived still post their results.
- [ ] Renames, archiving, unarchiving and removals are recorded in the audit log.
- [ ] The Delete button and the delete route are gone.
