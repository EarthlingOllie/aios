"""The tool library: capabilities that can be given to agents.

Tools are code, so unlike skills they aren't stored or versioned themselves. Each is registered
here under a stable key with a settings model, and an agent version stores which tools it has
and their settings (`agent_version_tools`). At run time each tool is built from those settings
and passed to the run.

To add a tool, write a module that calls `register(ToolSpec(...))` and import it below.
"""

from aios.tools import hybrid_search, run_python, web_search  # noqa: F401  (registers the tools)
from aios.tools.registry import (
    TOOLS,
    ToolError,
    ToolSpec,
    build_tool,
    check_stored_tool,
    register,
    stored_settings,
    validate_tool,
)

__all__ = [
    "TOOLS",
    "ToolError",
    "ToolSpec",
    "build_tool",
    "check_stored_tool",
    "register",
    "stored_settings",
    "validate_tool",
]
