# 07: Slack

**What to build:** Organisations can connect Slack. Admins pick channels the aios app can see, with guidance that private channels must invite the app first. Messages and thread replies sync with channel, author name and permalink; recent edits and deletions are re-read on every sync, a longer window daily, and a full resync re-reads everything. Agents can post to allowed channels with the post-to-Slack tool, signed with the agent's name and audited.

[Spec](../../specs/phase-4-inflow-and-insight.md)

**Blocked by:** [04: Connectors: credentials, allowlist, choosing what syncs, removal](04-connectors.md), [05: Connector tools and the MCP client](05-connector-tools-and-mcp.md)

**Status:** done

- [x] Recorded responses cover new messages, thread replies, and edits and deletions inside and outside each re-read window, plus a full resync.
- [x] A channel removed from scope, archived, or left by the app has its items removed while other channels carry on, and the channel is marked as needing attention on the connector page.
- [x] A rate-limited response slows the sync instead of failing it.
- [x] The scope picker explains that private channels must invite the app first.
- [x] Posting to a channel outside the tool's allowed list is refused and the model is told.
- [x] An allowed post is made as the bot, signed "— <agent name> via aios", returns its permalink and writes an audit entry.
- [x] The connector shows its health on the Data page.

Notes: Slack's tool is served by aios's own in-process MCP server (`aios.mcp_local`) over Slack's
Web API, rather than Slack's hosted MCP server, so the allowed-channel check, the signature and
the permalink are aios's. Replies to a thread whose first message is older than the re-read
window are found only by a full resync (Slack's history doesn't surface them). Public channels an
admin picks are joined at their first sync (scope `channels:join`); one the app later leaves is
not rejoined.
