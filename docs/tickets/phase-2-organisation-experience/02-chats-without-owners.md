# 02: Chats without owners, and archiving chats

**What to build:** Chats stop having owners, and all chat members are equal.

- **People join and leave.** Anyone can leave, including the last person; the chat stays. Nobody removes a person from a chat, admins included.
- **Any chat member** can rename a chat, change its description, and add or remove its agents and workflows.
- **Leaving the organisation** takes a person out of every chat, with nothing handed over.
- **Archiving.** Admins can archive and unarchive chats; that's their only extra power over chats. An archived chat is read-only, with a banner, but still found by search. The posting path checks for an archived chat before the Jev check from ticket 01. Runs already under way in it finish and post their results.
- **No delete.** Deleting a chat goes away.
- **Screens follow permissions.** The permission check returns the actions a person may take, and the screens show or hide them accordingly.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [01: Open chats, and the Jev check on posted messages](01-open-chats.md)

**Status:** ready-for-agent

- [ ] A small permission test through the API covers the rules: any chat member renames, describes, and adds or removes agents and workflows; a member who hasn't joined can't; only admins archive and unarchive.
- [ ] Removing a person from a chat is refused for everyone, admins included.
- [ ] The last person can leave, and the chat stays. Leaving the organisation hands nothing over.
- [ ] An archived chat refuses posts, mentions, joins, edits and adds with a 409 and a clear message, without calling Jev. Its messages still appear in search, and it's listed under an Archived filter.
- [ ] Runs already under way in a chat when it's archived still post their results.
- [ ] Renames, archiving and unarchiving are recorded in the audit log.
- [ ] The Delete button and the delete route are gone.
