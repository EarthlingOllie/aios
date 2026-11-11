"""The platform's models, one module per domain. Every name is re-exported here, so
`from aios.models import ...` reaches any of them, and `Base.metadata` sees every table (the
migrations' autogenerate and `alembic check` read it). What's in each organisation's store, its
agents, skills and workflows and their runs among it, is in `aios.datastores.models`."""

from aios.models.agents import (
    AgentRunStatus,
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
    Role,
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
    WorkflowNodeKind,
    WorkflowRunStatus,
    WorkflowStepStatus,
)

__all__ = [
    "Base",
    "User",
    "Organisation",
    "OrganisationDeletion",
    "OrganisationInvite",
    "OrganisationMember",
    "Role",
    "DataStoreStatus",
    "DataStore",
    "HistoryExportKind",
    "HistoryExport",
    "AgentRunStatus",
    "WorkflowNodeKind",
    "WorkflowRunStatus",
    "WorkflowStepStatus",
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
