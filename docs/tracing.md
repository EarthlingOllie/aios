# Tracing

Agent runs (the documents assistant's too) are traced to [Langfuse](https://langfuse.com) (open
source, self-hostable) through its LangChain callback handler (`aios.tracing`), when both keys
are set:

```sh
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://langfuse.example.com   # your instance; default http://localhost:3000
LANGFUSE_ENVIRONMENT=production                  # optional
```

Each trace carries the organisation (`organisation_id`) and the member who started the run
(`created_by`, also the Langfuse user; a schedule's run has neither a member nor a Langfuse
user), the run's thread id as the Langfuse session, tags for the agent and version, and the run
metadata: `agent_id`, `agent_version`, `agent_version_id`, the skill versions and the tools with
their settings (see [Agents and skills](agents-and-skills.md#versions)). A workflow's agent
node's trace carries its workflow run's starter, with the workflow, its version, the run and the
node's path (see [Workflows](workflows.md#runs)). A chat mention's handoff (see
[Chats](chats.md#the-handoff-brief)) is traced too, tagged `handoff`, with `invocation_id`, the
chat, the message, who asked (as `created_by` and the Langfuse user, with the organisation) and
the chat member as metadata, and `chat-<id>` as the session. With no keys, nothing is traced.
Traces are sent in the background and flushed when the API shuts down.
