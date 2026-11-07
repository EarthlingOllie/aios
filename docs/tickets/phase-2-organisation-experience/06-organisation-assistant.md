# 06: Organisation assistant and default chat agents

**What to build:** The documents assistant becomes the organisation assistant.

- **What it can do.** It searches everything in the store (documents, chats, past runs, synced content) and queries data tables. Its instructions tell it to search before answering, cite sources, query for figures, treat results as data, and say plainly when nothing answers.
- **When it's made.** With the organisation, not lazily with the first chat.
- **Upgrading existing ones.** A migration gives each existing documents assistant a new version, renaming it only if it still has the default name. It also makes an assistant for organisations that have none.
- **Default chat agents.** Admins choose an ordered list of agents that join every new chat, defaulting to the assistant.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [02: Chats without owners, and archiving chats](02-chats-without-owners.md), [Phase 0 · 12: Delete an organisation with a grace period; remove unscoped routes](../phase-0-refactor-and-guard-rails/12-delete-organisation.md), [Phase 1 · 06: Unified search across documents, chats and runs](../phase-1-foundation/06-unified-search.md)

**Status:** ready-for-agent

- [ ] A new organisation has the organisation assistant from the start. With the scripted model, it answers using both search and query data.
- [ ] The migration gives every documents assistant a new version, leaving pinned versions untouched.
  - [ ] An assistant still called "Documents assistant" becomes "Organisation assistant" if that name is free; renamed ones keep their name.
  - [ ] Organisations without an assistant get one; archived ones stay archived.
- [ ] Each organisation has at most one live built-in assistant, with archived rows excluded from that rule.
- [ ] Only admins can change the default chat agents setting, on the organisation's Settings tab.
- [ ] Default agents join new chats only, never direct messages. Archived agents are skipped and shown as archived on the setting.
- [ ] An agent the chat's creator can't invoke is still added, and that person's mentions of it are refused on the card.
