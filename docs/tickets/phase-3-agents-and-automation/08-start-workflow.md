# 08: start_workflow tool

**What to build:** Agents can hand structured work to automations. Editors give an agent a `start_workflow` tool listing the workflows it may start. The agent starts one with structured input, and gets the output straight back if the run finishes within the wait time. Otherwise the result is handed back to the conversation later.

[Spec](../../specs/phase-3-agents-and-automation.md)

**Blocked by:** [06: Structured workflows: typed I/O, templates, conditional edges](06-structured-workflows.md), [Phase 1 · 10: An invocation for every run, and turns as records](../phase-1-foundation/10-invocations-and-turns.md), [Phase 2 · 08: Handback, and delivery to chats and direct messages](../phase-2-organisation-experience/08-handback-and-delivery.md)

**Status:** ready-for-agent

- [ ] The tool's description lists the allowed workflows with their descriptions and input schemas.
- [ ] A run that finishes within the wait time returns its output, or its error, as the tool's result.
- [ ] A slow run returns its run id. Its result is handed back to the calling turn's conversation, else to the trigger's delivery chat, else only to the run page.
- [ ] Refusals come back as the tool's result: input that doesn't fit the schema, invocable by, guards, and an archived or deleted target. An archived workflow refuses only that call.
- [ ] Starting a workflow already in the chain of causes is refused, and so is exceeding the maximum depth.
- [ ] The run's invocation is caused by `start_workflow` and chained to the calling turn's invocation.
