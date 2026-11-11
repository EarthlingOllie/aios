# aios docs

How aios works today, one page per domain. Start with the [README](../README.md) for the overview,
the architecture and a quick start. The words are the glossary's, [`CONTEXT.md`](../CONTEXT.md):
"organisation", "store", "run", "turn", "invocation" and so on mean what it says they mean.

| Page | What it covers |
| --- | --- |
| [Accounts and organisations](accounts-and-organisations.md) | Registering and signing in, sessions and rate limits; organisations, what they own, invitation links and leaving. |
| [Stores](stores.md) | Each organisation's own database: the database roles, provisioning, uploaded files, the Data page's tables, quotas, dropped tables and columns, and the run history export. |
| [Agents and skills](agents-and-skills.md) | Defining agents and skills, their versions, agent runs and their history, the documents assistant, and deleting an agent. |
| [Tools](tools.md) | The tool library: how tools are registered, web search, Run Python, the data tools, schema change requests, and the Search page. |
| [Workflows](workflows.md) | Graphs of agents and workflows: the editor, versions, nesting, and how a workflow run is run. |
| [Chats](chats.md) | Group conversations: chat members, visibility, unread counts, messages, mentions, invocations and the handoff brief. |
| [Triggers](triggers.md) | Schedules, the scheduler, and the firing machinery that runs agent turns outside a request. |
| [Development](development.md) | Prerequisites, the code layout, running the stack in Docker or locally, debugging with Claude Code, checks and tests, the dependency policy, changing models, and image size notes. |
| [Deploy](deploy.md) | Deploying to AWS with Terraform and `infra/deploy.sh`, the database secrets, and sandboxes on Fargate. |
| [Tracing](tracing.md) | Tracing agent runs and handoffs to Langfuse. |

Also in `docs/`:

- [Decision records](decisions/): the decisions that shape aios, starting with
  [0001](decisions/0001-organisation-database-as-system-of-record.md).
- [Roadmap](roadmap.md): what aios will need later, and why each item waits.
- [Specs](specs/) and [tickets](tickets/README.md): the work in progress, phase by phase.

Keep a page to how the code behaves now; what's planned goes in a spec or the roadmap.
