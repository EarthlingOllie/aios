"""Export and import: an agent or workflow version as a file, and new copies of one in another
organisation.

**The file** is JSON in `ExportFile`'s shape: a format name and version, when it was exported,
which entry is the root, and the skills, agents and workflows it holds, each once, keyed by a
reference local to the file (`skill-1`, `agent-2`, ...). An agent holds its configuration (name,
description, model, base prompt, tools with their settings) and refers to the exact skill
versions it pins, whose content the file holds; a workflow holds its name, description, layout,
nodes (each referring to an agent or workflow in the file) and edges. Versions are exported, so
an agent pinned at two versions is two entries.

What never travels: ids, data, memory, runs, triggers, chats, credentials, provenance (who made
or changed what, and when, and version notes) and the built-in marker, so an exported
organisation assistant comes back as an ordinary agent.

**Importing** (`plan`, then `apply`) makes new rows at version 1, unconnected to anything in the
organisation: skills are always copied, never matched to the organisation's own by name. A name
already taken (archived rows' included, or earlier in the same file) gets " (imported)", then
" (imported 2)" and so on. A tool the organisation can't use (missing from the library, or its
settings refused) doesn't stop the import: the agent version is saved with it and can't run,
which its page says (`unrunnable`), until it's changed.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, FiniteFloat, ValidationError, model_serializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from aios import approvals, audit
from aios.api.provenance import built_by
from aios.datastores.models import (
    Agent,
    AgentVersion,
    Skill,
    SkillVersion,
    Workflow,
    WorkflowVersion,
)
from aios.models import WorkflowNodeKind
from aios.tools import ToolError, ToolOrganisation, stored_settings, unusable_tools
from aios.versioning import (
    NodeSpec,
    save_agent_version,
    save_skill_version,
    save_workflow_version,
)
from aios.workflows import GraphError, validate_graph
from aios.workflows.structured import NodeDeclared, check_workflow

FORMAT = "aios-export"
FORMAT_VERSION = 1

Kind = Literal["skill", "agent", "workflow"]

_NAME = Field(min_length=1, max_length=255, pattern=r"\S")


class ExportedSkill(BaseModel):
    name: str = _NAME
    description: str = ""
    content: str = ""


class ExportedTool(BaseModel):
    # A key in the tool library (`aios.tools.TOOLS`), and its settings. Connector tools name
    # their connector by kind and name in their settings, never by a credential.
    key: str = Field(min_length=1, max_length=64)
    settings: dict[str, Any] = {}


class ExportedAgent(BaseModel):
    name: str = _NAME
    description: str = ""
    model: str | None = Field(default=None, max_length=255)
    base_prompt: str = ""
    tools: list[ExportedTool] = []
    # The skills it pins, in order, by their references in the file.
    skills: list[str] = []


class _Declared(BaseModel):
    """What's declared (schemas, templates, conditions) is in the file only where it's set, so a
    workflow without any exports as it always has."""

    @model_serializer(mode="wrap")
    def _only_declared(self, handler) -> dict[str, Any]:
        out = handler(self)
        for name in (
            "input_schema",
            "output_schema",
            "input_template",
            "condition",
            "prompt_template",
            "approver_role",
            "expiry_days",
        ):
            if name in out and out[name] is None:
                del out[name]
        return out


class ExportedNode(_Declared):
    key: str
    kind: WorkflowNodeKind
    # The agent or workflow it runs, by its reference in the file; None for an approval node.
    ref: str | None = None
    # What it declares, if anything (`aios.workflows.structured`).
    input_schema: Any = None
    output_schema: Any = None
    input_template: Any = None
    # An approval node's (`aios.approvals`).
    prompt_template: str | None = None
    approver_role: Literal["editor", "admin"] | None = None
    expiry_days: int | None = Field(default=None, ge=1, le=365)


class ExportedEdge(_Declared):
    source: str
    target: str
    condition: Any = None


class ExportedPosition(BaseModel):
    x: FiniteFloat
    y: FiniteFloat


class ExportedWorkflow(_Declared):
    name: str = _NAME
    description: str = ""
    # Where the editor draws each node, by key.
    layout: dict[str, ExportedPosition] = {}
    nodes: list[ExportedNode] = []
    edges: list[ExportedEdge] = []
    input_schema: Any = None
    output_schema: Any = None


class ExportedRoot(BaseModel):
    kind: Literal["agent", "workflow"]
    ref: str


class ExportFile(BaseModel):
    format: Literal["aios-export"]
    format_version: Literal[1]
    exported_at: datetime
    root: ExportedRoot
    skills: dict[str, ExportedSkill] = {}
    agents: dict[str, ExportedAgent] = {}
    workflows: dict[str, ExportedWorkflow] = {}


class NotAnExport(ValueError):
    """A file that isn't an export this app can import, and why."""


# --- Exporting ---------------------------------------------------------------------------------


@dataclass
class _Exporter:
    """Collects a version's skills, agents and workflows, each once, under local references."""

    skills: dict[int, str] = field(default_factory=dict)
    agents: dict[int, str] = field(default_factory=dict)
    workflows: dict[int, str] = field(default_factory=dict)
    entries: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {"skills": {}, "agents": {}, "workflows": {}}
    )

    def skill(self, version: SkillVersion) -> str:
        if version.id not in self.skills:
            ref = f"skill-{len(self.skills) + 1}"
            self.skills[version.id] = ref
            self.entries["skills"][ref] = ExportedSkill(
                name=version.name, description=version.description, content=version.content
            )
        return self.skills[version.id]

    def agent(self, version: AgentVersion) -> str:
        if version.id not in self.agents:
            ref = f"agent-{len(self.agents) + 1}"
            self.agents[version.id] = ref
            agent = version.agent
            self.entries["agents"][ref] = ExportedAgent(
                name=agent.name,
                description=agent.description,
                model=version.model,
                base_prompt=version.base_prompt,
                tools=[ExportedTool(key=t.tool_key, settings=t.config) for t in version.tools],
                skills=[self.skill(s) for s in version.skills],
            )
        return self.agents[version.id]

    def workflow(self, version: WorkflowVersion) -> str:
        if version.id not in self.workflows:
            nodes = []
            for node in version.nodes:
                if node.kind == WorkflowNodeKind.agent:
                    ref = self.agent(node.agent_version)
                elif node.kind == WorkflowNodeKind.workflow:
                    ref = self.workflow(node.subworkflow_version)
                else:
                    ref = None
                nodes.append(
                    ExportedNode(
                        key=node.key,
                        kind=node.kind,
                        ref=ref,
                        input_schema=node.input_schema,
                        output_schema=node.output_schema,
                        input_template=node.input_template,
                        prompt_template=node.prompt_template,
                        approver_role=node.approver_role,
                        expiry_days=node.expiry_days,
                    )
                )
            # Numbered once its nested workflows are, so they come first in the file.
            ref = f"workflow-{len(self.workflows) + 1}"
            self.workflows[version.id] = ref
            workflow = version.workflow
            keys = {n.key for n in version.nodes}
            self.entries["workflows"][ref] = ExportedWorkflow(
                name=workflow.name,
                description=workflow.description,
                layout={k: p for k, p in (workflow.layout or {}).items() if k in keys},
                nodes=nodes,
                edges=[
                    ExportedEdge(source=e.source, target=e.target, condition=e.condition)
                    for e in version.edges
                ],
                input_schema=version.input_schema,
                output_schema=version.output_schema,
            )
        return self.workflows[version.id]

    def done(self, kind: Literal["agent", "workflow"], ref: str) -> dict[str, Any]:
        return ExportFile(
            format=FORMAT,
            format_version=FORMAT_VERSION,
            exported_at=datetime.now(UTC),
            root=ExportedRoot(kind=kind, ref=ref),
            **self.entries,
        ).model_dump(mode="json")


def export_agent(version: AgentVersion) -> dict[str, Any]:
    """The file for an agent version: it and the skill versions it pins."""
    exporter = _Exporter()
    return exporter.done("agent", exporter.agent(version))


def export_workflow(version: WorkflowVersion) -> dict[str, Any]:
    """The file for a workflow version: it, and every agent, skill and workflow version it runs
    at any depth, each once."""
    exporter = _Exporter()
    return exporter.done("workflow", exporter.workflow(version))


# --- Importing ---------------------------------------------------------------------------------


class Planned(BaseModel):
    """One thing an import makes (or a dry run would)."""

    kind: Kind
    # Its reference in the file.
    ref: str
    # Its name in the file, and the name it's made with: different if that was taken.
    name: str
    imported_name: str
    renamed: bool
    # Why its version can't run here, if it can't (an agent's missing tool, say).
    unrunnable: str | None = None
    # Its id once made; None on a dry run.
    id: int | None = None


@dataclass
class Plan:
    file: ExportFile
    skills: dict[str, Planned]
    agents: dict[str, Planned]
    # In an order that makes each nested workflow before the workflows that run it.
    workflows: dict[str, Planned]
    # Each agent's tools as they're saved, by its reference.
    tools: dict[str, list[tuple[str, dict[str, Any]]]]

    @property
    def root(self) -> Planned:
        root = self.file.root
        return (self.agents if root.kind == "agent" else self.workflows)[root.ref]

    @property
    def items(self) -> list[Planned]:
        return [*self.skills.values(), *self.agents.values(), *self.workflows.values()]


def parse(data: bytes) -> ExportFile:
    """The file, checked against the format and its references; `NotAnExport` if it fails."""
    try:
        file = ExportFile.model_validate_json(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(map(str, first["loc"]))
        raise NotAnExport(f"{where}: {first['msg']}" if where else first["msg"]) from exc
    root = file.root
    if root.ref not in (file.agents if root.kind == "agent" else file.workflows):
        raise NotAnExport(f"The root {root.kind} {root.ref!r} isn't in the file.")
    for ref, agent in file.agents.items():
        for skill in agent.skills:
            if skill not in file.skills:
                raise NotAnExport(f"Agent {ref!r} refers to a skill that isn't in the file.")
        if len(set(agent.skills)) != len(agent.skills):
            raise NotAnExport(f"Agent {ref!r} has a skill more than once.")
        if len({t.key for t in agent.tools}) != len(agent.tools):
            raise NotAnExport(f"Agent {ref!r} has a tool more than once.")
    for ref, workflow in file.workflows.items():
        for node in workflow.nodes:
            if node.kind == WorkflowNodeKind.approval:
                if node.ref is not None or not (node.prompt_template or "").strip():
                    raise NotAnExport(
                        f"Workflow {ref!r} has an approval node without a prompt, or that"
                        " refers to something."
                    )
                continue
            pool = file.agents if node.kind == WorkflowNodeKind.agent else file.workflows
            if node.ref not in pool:
                raise NotAnExport(
                    f"Workflow {ref!r} has a node that refers to a {node.kind} not in the file."
                )
        try:
            validate_graph(
                [n.key for n in workflow.nodes], [(e.source, e.target) for e in workflow.edges]
            )
        except GraphError as exc:
            raise NotAnExport(f"Workflow {ref!r}: {exc}") from exc
        errors = check_workflow(
            [
                NodeDeclared(
                    n.key,
                    n.input_schema,
                    approvals.OUTCOME_SCHEMA
                    if n.kind == WorkflowNodeKind.approval
                    else n.output_schema
                    if n.output_schema is not None or n.kind == WorkflowNodeKind.agent
                    else file.workflows[n.ref].output_schema,
                    n.input_template,
                )
                for n in workflow.nodes
            ],
            [(e.source, e.target, e.condition) for e in workflow.edges],
            workflow.input_schema,
            workflow.output_schema,
        )
        if errors:
            item, message = errors[0]
            raise NotAnExport(f"Workflow {ref!r}: {item}: {message}")
    _nesting_order(file)
    return file


def _nesting_order(file: ExportFile) -> list[str]:
    """The file's workflows, each after those it runs; `NotAnExport` if one runs itself."""
    order: list[str] = []
    state: dict[str, str] = {}

    def visit(ref: str) -> None:
        if state.get(ref) == "done":
            return
        if state.get(ref) == "visiting":
            raise NotAnExport(f"Workflow {ref!r} runs itself.")
        state[ref] = "visiting"
        for node in file.workflows[ref].nodes:
            if node.kind == WorkflowNodeKind.workflow:
                visit(node.ref)
        state[ref] = "done"
        order.append(ref)

    for ref in file.workflows:
        visit(ref)
    return order


def imported_name(name: str, taken: set[str]) -> str:
    """`name`, or the first of "name (imported)", "name (imported 2)", ... not in `taken`,
    shortened to fit 255 characters if it must be."""
    if name not in taken:
        return name
    n = 1
    while True:
        suffix = " (imported)" if n == 1 else f" (imported {n})"
        candidate = name[: 255 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        n += 1


def _names(store: Session, column) -> set[str]:
    return set(store.scalars(select(column)))


def _settings(key: str, settings: dict[str, Any]) -> dict[str, Any]:
    """A tool's settings as an import saves them: read leniently, as a stored version's are
    (fields the tool no longer has dropped, new ones defaulted); as they are if they can't be
    (an unknown tool, or a value its settings refuse), so the version says why it can't run."""
    try:
        return stored_settings(key, settings).model_dump(mode="json")
    except ToolError:
        return settings


def plan(store: Session, file: ExportFile, org: ToolOrganisation) -> Plan:
    """What importing the file into the organisation (its store, `store`) makes: names, and
    whether each agent version can run here. Writes nothing."""
    names = {
        "skill": _names(store, Skill.name),
        "agent": _names(store, Agent.name),
        "workflow": _names(store, Workflow.name),
    }

    def planned(kind: Kind, ref: str, name: str, unrunnable: str | None = None) -> Planned:
        new = imported_name(name, names[kind])
        names[kind].add(new)
        return Planned(
            kind=kind,
            ref=ref,
            name=name,
            imported_name=new,
            renamed=new != name,
            unrunnable=unrunnable,
        )

    skills = {ref: planned("skill", ref, s.name) for ref, s in file.skills.items()}
    agents: dict[str, Planned] = {}
    tools: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for ref, agent in file.agents.items():
        tools[ref] = [(t.key, _settings(t.key, t.settings)) for t in agent.tools]
        reason = unusable_tools(tools[ref], org)
        agents[ref] = planned(
            "agent", ref, agent.name, f"This version can't run: {reason}" if reason else None
        )
    workflows = {}
    for ref in _nesting_order(file):
        workflow = file.workflows[ref]
        broken = sorted(
            {
                f"the {n.kind} {pool[n.ref].imported_name}"
                for n in workflow.nodes
                if n.kind != WorkflowNodeKind.approval
                for pool in [agents if n.kind == WorkflowNodeKind.agent else workflows]
                if pool[n.ref].unrunnable
            }
        )
        unrunnable = f"This version can't run: {', '.join(broken)} can't." if broken else None
        workflows[ref] = planned("workflow", ref, workflow.name, unrunnable)
    return Plan(file=file, skills=skills, agents=agents, workflows=workflows, tools=tools)


def apply(store: Session, the_plan: Plan, user_id: int, filename: str) -> None:
    """Make what the plan says, in the caller's store transaction (which the caller commits),
    each at version 1 and noted with the file's name, and audit each as imported. Fills in the
    plan's ids."""
    file = the_plan.file
    note = f"Imported from {filename}"
    skill_versions: dict[str, SkillVersion] = {}
    for ref, planned in the_plan.skills.items():
        exported = file.skills[ref]
        skill = Skill(**built_by(user_id), name=planned.imported_name)
        store.add(skill)
        skill_versions[ref] = save_skill_version(
            store,
            skill,
            description=exported.description,
            content=exported.content,
            note=note,
            user_id=user_id,
        )
        _done(store, user_id, "skill", skill, planned, filename)
    agent_versions: dict[str, AgentVersion] = {}
    for ref, planned in the_plan.agents.items():
        exported = file.agents[ref]
        agent = Agent(
            **built_by(user_id), name=planned.imported_name, description=exported.description
        )
        store.add(agent)
        agent_versions[ref] = save_agent_version(
            store,
            agent,
            base_prompt=exported.base_prompt,
            skills=[skill_versions[s] for s in exported.skills],
            tools=the_plan.tools[ref],
            model=exported.model,
            note=note,
            user_id=user_id,
        )
        _done(store, user_id, "agent", agent, planned, filename)
    workflow_versions: dict[str, WorkflowVersion] = {}
    for ref, planned in the_plan.workflows.items():
        exported = file.workflows[ref]
        keys = {n.key for n in exported.nodes}
        workflow = Workflow(
            **built_by(user_id),
            name=planned.imported_name,
            description=exported.description,
            layout={k: p.model_dump() for k, p in exported.layout.items() if k in keys},
        )
        store.add(workflow)
        workflow_versions[ref] = save_workflow_version(
            store,
            workflow,
            nodes=[
                NodeSpec(
                    key=n.key,
                    kind=n.kind,
                    version_id=(
                        agent_versions[n.ref].id
                        if n.kind == WorkflowNodeKind.agent
                        else workflow_versions[n.ref].id
                        if n.kind == WorkflowNodeKind.workflow
                        else None
                    ),
                    input_schema=n.input_schema,
                    output_schema=n.output_schema,
                    input_template=n.input_template,
                    prompt_template=n.prompt_template,
                    approver_role=(n.approver_role or "editor")
                    if n.kind == WorkflowNodeKind.approval
                    else None,
                    expiry_days=n.expiry_days,
                )
                for n in exported.nodes
            ],
            edges=[(e.source, e.target) for e in exported.edges],
            note=note,
            user_id=user_id,
            conditions={(e.source, e.target): e.condition for e in exported.edges},
            input_schema=exported.input_schema,
            output_schema=exported.output_schema,
        )
        store.flush()
        _done(store, user_id, "workflow", workflow, planned, filename)


def _done(
    store: Session, user_id: int, kind: Kind, thing: Any, planned: Planned, filename: str
) -> None:
    audit.configured(store, user_id, kind, thing, "imported", file=filename)
    planned.id = thing.id
