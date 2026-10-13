"""The chat model agents run on. Model names are LangChain "provider:model" strings."""

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from aios.config import get_settings

# Models whose web search filters results with code execution (`web_search_20260209`); others
# get the basic variant. Anthropic's model names, without the "anthropic:" prefix.
_DYNAMIC_WEB_SEARCH = (
    "claude-fable-5",
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
    """
    return init_chat_model(model_name(name))


def web_search_type(name: str | None = None) -> str:
    """The Anthropic web search tool version `name` supports."""
    model = model_name(name).removeprefix("anthropic:")
    if model.startswith(_DYNAMIC_WEB_SEARCH):
        return "web_search_20260209"
    return "web_search_20250305"
