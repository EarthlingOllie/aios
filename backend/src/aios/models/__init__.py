"""The platform's models, one module per domain. Every name is re-exported here, so
`from aios.models import ...` reaches any of them, and `Base.metadata` sees every table (the
migrations' autogenerate and `alembic check` read it)."""

from aios.models.agents import (
    Agent,
    AgentRun,
    AgentRunStatus,
    AgentVersion,
    AgentVersionTool,
    SandboxSession,
    Skill,
    SkillVersion,
    agent_version_skills,
)
from aios.models.approvals import (
    SchemaChangeRequest,
    SchemaChangeStatus,
)
from aios.models.base import (
    Base,
)
from aios.models.chats import (
    Chat,
    ChatAuthorKind,
    ChatInvocation,
    ChatInvocationStatus,
    ChatMember,
    ChatMemberKind,
    ChatMemberRole,
    ChatMessage,
    ChatMessageKind,
    ChatVisibility,
)
from aios.models.identity import (
    Organisation,
    OrganisationDeletion,
    OrganisationInvite,
    OrganisationMember,
    User,
)
from aios.models.notifications import (
    Notification,
)
from aios.models.stores import (
    DataStore,
    DataStoreStatus,
    HistoryExport,
    HistoryExportKind,
)
from aios.models.triggers import (
    Trigger,
    TriggerKind,
    TriggerStatus,
    TriggerTargetKind,
)
from aios.models.workflows import (
    Workflow,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunStep,
    WorkflowStepStatus,
    WorkflowVersion,
)

__all__ = [
    "Base",
    "User",
    "Organisation",
    "OrganisationDeletion",
    "OrganisationInvite",
    "OrganisationMember",
    "DataStoreStatus",
    "DataStore",
    "HistoryExportKind",
    "HistoryExport",
    "Skill",
    "SkillVersion",
    "agent_version_skills",
    "Agent",
    "AgentVersion",
    "AgentVersionTool",
    "AgentRunStatus",
    "AgentRun",
    "SandboxSession",
    "WorkflowNodeKind",
    "Workflow",
    "WorkflowVersion",
    "WorkflowNode",
    "WorkflowEdge",
    "WorkflowRunStatus",
    "WorkflowStepStatus",
    "WorkflowRun",
    "WorkflowRunStep",
    "TriggerKind",
    "TriggerTargetKind",
    "TriggerStatus",
    "Trigger",
    "SchemaChangeStatus",
    "SchemaChangeRequest",
    "Notification",
    "ChatVisibility",
    "ChatMemberKind",
    "ChatMemberRole",
    "Chat",
    "ChatMember",
    "ChatAuthorKind",
    "ChatMessageKind",
    "ChatMessage",
    "ChatInvocationStatus",
    "ChatInvocation",
]
