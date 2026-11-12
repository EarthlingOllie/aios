"""The platform's models, one module per domain. Every name is re-exported here, so
`from aios.models import ...` reaches any of them, and `Base.metadata` sees every table (the
migrations' autogenerate and `alembic check` read it). What's in each organisation's store, its
agents, skills, workflows, their runs, schedules, chats, invocations and schema change requests
among it, is in `aios.datastores.models`."""

from aios.models.agents import (
    AgentRunStatus,
)
from aios.models.approvals import (
    ApprovalKind,
    ApprovalStatus,
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
    ChatMessageKind,
    InvocationCause,
    InvocationStatus,
)
from aios.models.connectors import (
    ConnectorCredential,
    CredentialKind,
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
    AgentStoreRole,
    DataStore,
    DataStoreStatus,
    StoreUsageSample,
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
    "StoreUsageSample",
    "DataStore",
    "AgentStoreRole",
    "ConnectorCredential",
    "CredentialKind",
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
    "ApprovalKind",
    "ApprovalStatus",
    "Notification",
    "ChatMemberKind",
    "ChatAuthorKind",
    "ChatMessageKind",
    "InvocationCause",
    "InvocationStatus",
    "InvocableBy",
    "UsageKind",
    "UsagePurpose",
]
