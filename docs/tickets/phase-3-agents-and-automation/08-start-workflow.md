# 08: start_workflow tool

**What to build:** Agents can hand structured work to automations. Editors give an agent a `start_workflow` tool listing the workflows it may start. The agent starts one with structured input, and gets the output straight back if the run finishes within the wait time. Otherwise the result is handed back to the conversation later.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [06: Structured workflows: typed I/O, templates, conditional edges](06-structured-workflows.md), [Phase 2 · 08: Handback, and delivery to chats](../phase-2-organisation-experience/08-handback-and-delivery.md)

**Status:** done

- [x] The tool's description lists the allowed workflows with their descriptions and input schemas.
- [x] A run that finishes within the wait time returns its output, or its error, as the tool's result.
- [x] A slow run returns its run id. Its result is handed back to the calling turn's conversation, else to the trigger's delivery chat, else only to the run page.
- [x] Refusals come back as the tool's result: input that doesn't fit the schema, invocable by, guards, and an archived or deleted target. An archived workflow refuses only that call. (The guards are a per-agent rate, `START_WORKFLOW_RATE_PER_AGENT`; the budget check is still the no-op `aios.guards.check_budget` until [10](10-budgets-and-allowances.md).)
- [x] Starting a workflow already in the chain of causes is refused, and so is exceeding the maximum depth.
- [x] The input is derived text: wherever it reaches a model in the started run, it's wrapped as untrusted.
- [x] The run's invocation is caused by `start_workflow` and chained to the calling turn's invocation.
