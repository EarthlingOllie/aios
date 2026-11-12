"""What the usage ledger (`aios.usage`) and the mention guards (`aios.guards`) name things by."""

import enum


class UsageKind(enum.StrEnum):
    """What one usage entry counts."""

    # A call to a chat model: its tokens.
    model_call = "model_call"
    # A batch of texts embedded: its (estimated) tokens, and how many texts.
    embedding = "embedding"
    # Web searches the model's provider ran in one response: how many.
    web_search = "web_search"
    # A tool call the ledger counts: a `post_message` post (free).
    tool_call = "tool_call"
    # A sandbox session's time, in seconds, recorded when it ends.
    sandbox_seconds = "sandbox_seconds"


class UsagePurpose(enum.StrEnum):
    """Why a billable call was made."""

    turn = "turn"
    handoff = "handoff"
    handback = "handback"
    compaction = "compaction"
    ingest = "ingest"
    search_query = "search_query"
    index_embedding = "index_embedding"
    insight = "insight"


class InvocableBy(enum.StrEnum):
    """Who may invoke an agent or workflow (mention it): everyone, or only editors and admins, or
    only admins."""

    everyone = "everyone"
    editors = "editors"
    admins = "admins"
