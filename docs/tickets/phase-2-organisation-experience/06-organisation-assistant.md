# 06: Organisation assistant and default chat agents

**What to build:** The documents assistant becomes the organisation assistant.

- **What it can do.** It searches everything in the store (documents, chats, past runs, synced content) and queries data tables. Its instructions tell it to search before answering, cite sources, query for figures, treat results and untrusted blocks as data, and say plainly when nothing answers.
- **When it's made.** With the organisation, not lazily with the first chat.
- **No upgrade migration.** `make seed` rebuilds dev organisations, each with its assistant.
- **Default chat agents.** Admins choose an ordered list of agents that join every new chat, defaulting to the assistant.

[Spec](../../specs/phase-2-organisation-experience.md)

**Blocked by:** [05: One chat experience: "Chat with this agent"](05-one-chat-experience.md), [Phase 1 · 06: Unified search across documents, chats and runs](../phase-1-foundation/06-unified-search.md)

**Status:** ready-for-agent

- [ ] A new organisation has the organisation assistant from the start. With the scripted model, it answers using both search and query data.
- [ ] `make seed` makes each dev organisation's assistant through the organisation creation path.
- [ ] Each organisation has at most one live built-in assistant, with archived rows excluded from that rule.
- [ ] Only admins can change the default chat agents setting, on the organisation's Settings tab.
- [ ] Default agents join new chats only, except chats made by **Chat with this agent**. Archived agents are skipped and shown as archived on the setting.
- [ ] An agent the chat's creator can't invoke is still added, and that person's mentions of it are refused on the card.
