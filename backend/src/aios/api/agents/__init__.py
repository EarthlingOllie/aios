"""The agents API: agents (`crud`), their versions (`versions`), their runs (`runs`), a run's
chat stream (`chat`) and their memories (`memories`), mounted as one router.

Every name is re-exported here, where callers import it. The turn's hooks (`load_chat_context`,
`_start_turn`, `_end_turn`, `_record_turn` and `ChatDeps`) are read here at call time, so a test
that replaces one here replaces it for the turn."""

from fastapi import APIRouter

from aios.agent import ChatDeps
from aios.api.agents import chat as _chat
from aios.api.agents import crud as _crud
from aios.api.agents import memories as _memories
from aios.api.agents import runs as _runs
from aios.api.agents import versions as _versions
from aios.api.agents.chat import (
    WAITING,
    ChatContext,
    Continue,
    Resume,
    ResumeFailed,
    RunWaiting,
    _begin_turn,
    _end_turn,
    _mark_resumed,
    _record_turn,
    _resume_command,
    _start_turn,
    load_chat_context,
    prewarm_sandbox,
    resume_run,
    run_chat,
    trace_metadata,
    turn_stream,
)
from aios.api.agents.common import (
    AGENT_NAME_INDEX,
    AgentIn,
    AgentOut,
    AgentSkillOut,
    AgentSummary,
    AgentToolIn,
    AgentToolOut,
    AgentVersionOut,
    agent_out,
    get_agent_version,
    get_org_agent,
    lock_agent,
    lock_live_agent,
    refuse_archived_skills,
    refuse_if_archived,
    resolve_skill_versions,
    resolve_tools,
    skill_count,
    skill_ids,
    tool_count,
    tool_keys,
    tool_out,
    version_out,
)
from aios.api.agents.crud import (
    archive_agent,
    create_agent,
    get_agent,
    get_agent_used_by,
    list_agents,
    unarchive_agent,
    update_agent,
)
from aios.api.agents.runs import (
    RunOut,
    RunSummary,
    build_version_tools,
    cancel_chat_requests,
    create_run,
    delete_run,
    get_org_run,
    get_run,
    list_runs,
    new_agent_run,
    run_chats,
    run_summaries,
    run_summary,
    run_summary_query,
)
from aios.api.agents.versions import (
    AgentVersionSummary,
    get_agent_version_detail,
    list_agent_versions,
    restore_agent_version,
)
from aios.usage import tool_call_count

# One router for the package, its sub-domains' routes in the order they were declared.
router = APIRouter()
router.include_router(_crud.router)
router.include_router(_versions.router)
router.include_router(_runs.router)
router.include_router(_chat.router)
router.include_router(_memories.router)

__all__ = [
    "AGENT_NAME_INDEX",
    "AgentIn",
    "AgentOut",
    "AgentSkillOut",
    "AgentSummary",
    "AgentToolIn",
    "AgentToolOut",
    "AgentVersionOut",
    "AgentVersionSummary",
    "ChatContext",
    "Continue",
    "ChatDeps",
    "Resume",
    "ResumeFailed",
    "RunOut",
    "RunSummary",
    "RunWaiting",
    "WAITING",
    "_begin_turn",
    "_end_turn",
    "_mark_resumed",
    "_record_turn",
    "_resume_command",
    "_start_turn",
    "agent_out",
    "build_version_tools",
    "cancel_chat_requests",
    "archive_agent",
    "create_agent",
    "create_run",
    "delete_run",
    "get_agent",
    "get_agent_used_by",
    "get_agent_version",
    "get_agent_version_detail",
    "get_org_agent",
    "get_org_run",
    "get_run",
    "list_agent_versions",
    "list_agents",
    "list_runs",
    "load_chat_context",
    "lock_agent",
    "lock_live_agent",
    "refuse_archived_skills",
    "refuse_if_archived",
    "new_agent_run",
    "prewarm_sandbox",
    "resolve_skill_versions",
    "resolve_tools",
    "restore_agent_version",
    "resume_run",
    "run_chat",
    "run_chats",
    "run_summaries",
    "run_summary",
    "run_summary_query",
    "skill_count",
    "skill_ids",
    "tool_call_count",
    "tool_count",
    "tool_keys",
    "tool_out",
    "trace_metadata",
    "turn_stream",
    "unarchive_agent",
    "update_agent",
    "version_out",
]
