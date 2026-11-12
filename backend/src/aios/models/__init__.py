"""The platform's models, one module per domain. Every name is re-exported here, so
`from aios.models import ...` reaches any of them, and `Base.metadata` sees every table (the
migrations' autogenerate and `alembic check` read it). What's in each organisation's store, its
agents, skills, workflows, their runs, schedules, chats, invocations and schema change requests
among it, is in `aios.datastores.models`."""

from aios.models.agents import (
    AgentRunStatus,
)
from aios.models.approvals import (
    SchemaChangeStatus,
)
from aios.models.audit import (
    AuditActorKind,
    AuditOutbox,
)
from aios.models.base import (
    Base,
)
from aios.models.chats import (
    ChatAuthorKind,
    ChatMemberKind,
    ChatMemberRole,
    ChatMessageKind,
    ChatVisibility,
    InvocationCause,
    InvocationStatus,
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
)
from aios.models.triggers import (
    EventType,
    ScheduleDue,
    TriggerKind,
    TriggerStatus,
    TriggerTargetKind,
)
from aios.models.usage import (
    InvocableBy,
    UsageKind,
    UsagePurpose,
)
from aios.models.work import (
    WorkItem,
    WorkKind,
    WorkLane,
)
from aios.models.workflows import (
    WorkflowNodeKind,
    WorkflowRunStatus,
    WorkflowStepStatus,
)

__all__ = [
    "Base",
    "AuditActorKind",
    "AuditOutbox",
    "User",
    "Organisation",
    "OrganisationDeletion",
    "OrganisationInvite",
    "OrganisationMember",
    "Role",
    "DataStoreStatus",
    "DataStore",
    "AgentRunStatus",
    "WorkflowNodeKind",
    "WorkflowRunStatus",
    "WorkflowStepStatus",
    "TriggerKind",
    "EventType",
    "TriggerTargetKind",
    "TriggerStatus",
    "ScheduleDue",
    "WorkItem",
    "WorkKind",
    "WorkLane",
    "SchemaChangeStatus",
    "Notification",
    "ChatVisibility",
    "ChatMemberKind",
    "ChatMemberRole",
    "ChatAuthorKind",
    "ChatMessageKind",
    "InvocationCause",
    "InvocationStatus",
    "InvocableBy",
    "UsageKind",
    "UsagePurpose",
]
