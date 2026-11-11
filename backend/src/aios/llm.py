"""The chat model agents run on. Model names are LangChain "provider:model" strings."""

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from aios.config import get_settings

# The scripted models' provider (`aios.testing.scenarios`), with scripted models enabled.
SCRIPTED = "scripted"

# Models whose web search filters results with code execution (`web_search_20260209`); others
# get the basic variant. Anthropic's model names, without the "anthropic:" prefix.
_DYNAMIC_WEB_SEARCH = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-mythos-preview",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)


def model_name(name: str | None = None) -> str:
    return name or get_settings().llm_model


def chat_model(name: str | None = None) -> BaseChatModel:
    """The chat model called `name`, or the default (`LLM_MODEL`).

    Built per run, so a missing API key fails that run rather than the app's startup. The
    provider reads its key (e.g. ANTHROPIC_API_KEY) from the environment.

    `scripted:<scenario>` is a scripted model (`aios.testing.scenarios`), only with scripted
    models enabled: otherwise it's an unknown provider, and nothing is called.
    """
    name = model_name(name)
    provider, _, scenario = name.partition(":")
    if provider == SCRIPTED:
        if not get_settings().scripted_models_enabled:
            raise ValueError(f"Unknown model provider {SCRIPTED!r} in {name!r}")
        from aios.testing import scenarios

        return scenarios.chat_model(scenario)
    return init_chat_model(name)


def web_search_type(name: str | None = None, *, dynamic: bool = True) -> str:
    """The Anthropic web search tool version `name` supports; the basic one if not `dynamic`."""
    model = model_name(name).removeprefix("anthropic:")
    if dynamic and model.startswith(_DYNAMIC_WEB_SEARCH):
        return "web_search_20260209"
    return "web_search_20250305"
