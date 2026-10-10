import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_ai.capabilities import WebSearch

from aios.tools.registry import ToolSpec, register

# What Anthropic accepts: a bare hostname, optionally followed by a path (where `*` is allowed),
# with no scheme or port. Anything else fails every request the agent makes, so it's rejected
# on save rather than breaking the runs pinned to that version.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"{_LABEL}(?:\.{_LABEL})+(?:/[^\s?#]*)?")


def _normalize(entry: str) -> str:
    # Hostnames are case-insensitive, paths aren't.
    host, slash, path = entry.strip().partition("/")
    return host.lower() + slash + path


class WebSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Each search is billed by the provider. Anthropic enforces the cap per model request, and
    # one turn can make several (tool calls, pause_turn continuations), so it does not cap a turn.
    max_uses: int = Field(
        5,
        ge=1,
        le=20,
        title="Max searches",
        description="Per model request; one turn can make several. Each search is billed.",
    )
    allowed_domains: list[str] = Field(
        [],
        max_length=50,
        title="Only these domains",
        description="Leave empty to search the whole web.",
    )
    blocked_domains: list[str] = Field(
        [],
        max_length=50,
        title="Never these domains",
        description="Can't be combined with the above.",
    )

    @field_validator("allowed_domains", "blocked_domains")
    @classmethod
    def clean_domains(cls, domains: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(d for d in map(_normalize, domains) if d))
        for domain in cleaned:
            if not _DOMAIN.fullmatch(domain):
                raise ValueError(
                    f"{domain!r} isn't a domain; use one like example.com or example.com/blog,"
                    " without https://"
                )
        return cleaned

    @model_validator(mode="after")
    def allow_or_block(self) -> "WebSearchConfig":
        # Anthropic rejects requests that set both.
        if self.allowed_domains and self.blocked_domains:
            raise ValueError("set either allowed or blocked domains, not both")
        return self


def build(config: WebSearchConfig) -> WebSearch:
    # The provider's native search (Anthropic's server-side web search tool); a model without
    # one fails the turn rather than silently running without search.
    return WebSearch(
        max_uses=config.max_uses,
        allowed_domains=config.allowed_domains or None,
        blocked_domains=config.blocked_domains or None,
    )


register(
    ToolSpec(
        key="web_search",
        name="Web search",
        description="Search the web for current information, run by the model provider.",
        config_model=WebSearchConfig,
        build=build,
    )
)
