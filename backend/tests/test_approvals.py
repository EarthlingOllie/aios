"""Approvals as one concept (`aios.approvals`, `aios.api.approvals`): schema changes and
workflow approval steps in one list, decided through one API, and the approval node.

A workflow's approval node pauses its run for a person in its role: reaching it renders its
prompt, records an approval with the prompt and its input, notifies only those who may decide
it, and waits. Approving or rejecting it (or its expiring, which counts as not approved) resumes
the run, and the node outputs the outcome, which its edges branch on. Cancelling the run cancels
the approval and clears its notifications. Every decision is audited.

Through the API as a signed-in person, with the scripted model, and the run service draining in
process as each request wakes it (`conftest.RunService`)."""

from datetime import timedelta

import pytest
from conftest import join_organisation, store_of
from sqlalchemy import func, select, update
from test_audit_log import entries
from test_chats import signed_in_as
from test_data_tools import provisioned
from test_permissions import set_role
from test_workflows import agent_node, make_agent

from aios import worker
from aios.datastores import schema_changes, store_session
from aios.datastores.models import SchemaChangeRequest
from aios.datastores.schema_changes import SchemaChange
from aios.models import DataStore, Notification, Role
from aios.testing.fake_model import ToolCall, instructions, model, use_model

RISK = {
    "type": "object",
    "properties": {"risk": {"type": "string"}, "amount": {"type": "number"}},
    "required": ["risk", "amount"],
}
CONTACTS = {
    "op": "create_table",
    "name": "contacts",
    "columns": [{"name": "name", "type": "text", "nullable": False}],
}


class Script:
    """The model. `classify` gives `answer` as structured output; any other agent answers
    `<name>(<message>)`. Records what each agent was sent."""

    def __init__(self, answer: dict | None = None):
        self.answer = answer or {"risk": "high", "amount": 250}
        self.seen: dict[str, list[str]] = {}

    async def __call__(self, messages, info):
        name = instructions(messages)
        self.seen.setdefault(name, []).append(messages[-1].content)
        if name == "classify":
            yield ToolCall("workflow_output", self.answer)
            return
        yield f"{name}({messages[-1].content})"


@pytest.fixture
def store(db, user) -> DataStore:
    return provisioned(db, store_of(user))


@pytest.fixture
def editor(db, user):
    return join_organisation(db, user, "editor")


@pytest.fixture
def participant(db, user):
    member = join_organisation(db, user, "participant")
    set_role(db, member, Role.participant, of=user)
    return member


def approval_node(key: str, prompt: str, **extra) -> dict:
    return {"key": key, "kind": "approval", "prompt_template": prompt, **extra}


def edge(source: str, target: str, condition=None) -> dict:
    return {"source": source, "target": target, "condition": condition}


APPROVED = {"==": [{"var": "output.approved"}, True]}
NOT_APPROVED = {"!": [{"var": "output.approved"}]}


def spend_workflow(
    api, *, role: str | None = "admin", name: str = "Spend", agents=None, **approval
) -> dict:
    """classify (structured: risk and amount) → approve, only when the risk is high → pay when
    approved, or tell when not. A low risk goes straight to `auto`."""
    agents = agents or make_agents(api)
    nodes = [
        {**agent_node("classify", agents["classify"]), "output_schema": RISK},
        approval_node(
            "approve",
            "Spend {{nodes.classify.amount}} on {{input}}?\nThe risk is {{nodes.classify.risk}}.",
            **({"approver_role": role} if role else {}),
            **approval,
        ),
        agent_node("pay", agents["pay"]),
        agent_node("tell", agents["tell"]),
        agent_node("auto", agents["auto"]),
    ]
    high = {"==": [{"var": "output.risk"}, "high"]}
    edges = [
        edge("classify", "approve", high),
        edge("classify", "auto", {"!": [high]}),
        edge("approve", "pay", APPROVED),
        edge("approve", "tell", NOT_APPROVED),
    ]
    resp = api.post("/api/workflows", json={"name": name, "nodes": nodes, "edges": edges})
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_agents(api) -> dict[str, dict]:
    return {n: make_agent(api, n, base_prompt=n) for n in ("classify", "pay", "tell", "auto")}


def start(api, workflow: dict, script: Script, text: str = "laptops") -> dict:
    with use_model(model(script)):
        resp = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": text})
    assert resp.status_code == 201, resp.text
    return read(api, workflow, resp.json()["id"])


def read(api, workflow: dict, run_id: int) -> dict:
    resp = api.get(f"/api/workflows/{workflow['id']}/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def steps(run: dict) -> dict[str, dict]:
    return {"/".join(s["path"]): s for s in run["steps"]}


def decide(api, run_service, script: Script, approval: dict, decision: str, **body):
    """Approve or reject the approval, then run what that enqueued, as the run service would."""
    with use_model(model(script)):
        resp = api.post(f"/api/approvals/{approval['id']}/{decision}", json=body)
        run_service.drain()
    return resp


def the_approval(api, **filters) -> dict:
    [found] = api.get("/api/approvals", params={"kind": "workflow_step", **filters}).json()
    return found


def notified(db, approval_id) -> dict[int, bool]:
    """Who the approval notified, and whether each notification is still unread."""
    db.expire_all()
    return {
        user_id: read_at is None
        for user_id, read_at in db.execute(
            select(Notification.user_id, Notification.read_at).where(
                Notification.kind == "approval_needed",
                Notification.ref["approval_id"].astext == str(approval_id),
            )
        )
    }


# --- Saving -------------------------------------------------------------------------------------


def test_an_approval_node_is_saved_with_its_prompt_role_and_expiry(api, store):
    workflow = spend_workflow(api, expiry_days=2)

    approve = next(n for n in workflow["current_version"]["nodes"] if n["key"] == "approve")
    assert approve["kind"] == "approval"
    assert approve["version_id"] is None
    assert approve["prompt_template"].startswith("Spend {{nodes.classify.amount}}")
    assert (approve["approver_role"], approve["expiry_days"]) == ("admin", 2)


def test_an_approval_node_decides_by_editors_by_default(api, store):
    workflow = spend_workflow(api, role=None)

    approve = next(n for n in workflow["current_version"]["nodes"] if n["key"] == "approve")
    assert approve["approver_role"] == "editor"


def test_saving_refuses_a_bad_approval_node_naming_each_item(api, store):
    agent = make_agent(api, "one", base_prompt="one")
    nodes = [
        agent_node("one", agent),
        approval_node("ask", "{{nodes.later.x}}", output_schema={"type": "object"}),
        approval_node("blank", " "),
        {**agent_node("two", agent), "prompt_template": "Hi", "approver_role": "admin"},
        approval_node("later", "Go?"),
    ]
    edges = [
        edge("one", "ask"),
        edge("ask", "later", {"==": [{"var": "output.nope"}, 1]}),
    ]
    resp = api.post("/api/workflows", json={"name": "Bad", "nodes": nodes, "edges": edges})

    assert resp.status_code == 422, resp.text
    items = {e["item"] for e in resp.json()["detail"]["errors"]}
    assert items == {
        "nodes.ask.output_schema",
        "nodes.ask.prompt_template",
        "nodes.blank.prompt_template",
        "nodes.two.prompt_template",
        "nodes.two.approver_role",
        "edges.ask→later.condition",
    }


@pytest.mark.parametrize("role", ["participant", "owner"])
def test_an_approval_node_takes_only_editor_or_admin(api, store, role):
    resp = api.post(
        "/api/workflows",
        json={"name": "Odd", "nodes": [approval_node("ask", "Go?", approver_role=role)]},
    )
    assert resp.status_code == 422


# --- Pausing, deciding and resuming ---------------------------------------------------------------


def test_a_run_branches_on_structured_output_pauses_at_the_approval_and_resumes_approved(
    api, db, user, store, run_service, editor
):
    workflow = spend_workflow(api)
    script = Script()

    waiting = start(api, workflow, script)

    assert waiting["status"] == "waiting"
    at = steps(waiting)
    assert at["classify"]["output_json"] == {"risk": "high", "amount": 250}
    assert at["auto"]["status"] == "skipped"
    assert at["approve"]["status"] == "waiting"
    assert "pay" not in at and "tell" not in at
    approval = the_approval(api)
    assert at["approve"]["approval_id"] == approval["id"]
    assert approval["kind"] == "workflow_step"
    assert approval["status"] == "pending"
    # Approvers see the full reason (the rendered prompt) and the input.
    assert approval["reason"] == "Spend 250 on laptops?\nThe risk is high."
    assert approval["summary"] == "Spend 250 on laptops?"
    assert approval["input"] == {"risk": "high", "amount": 250}
    assert approval["approver_role"] == "admin"
    assert approval["step"] == {
        "workflow_id": workflow["id"],
        "workflow_name": "Spend",
        "run_id": waiting["id"],
        "step_id": at["approve"]["id"],
        "path": ["approve"],
    }
    # Only those who may decide it are notified: the admin, not the editor.
    assert notified(db, approval["id"]) == {user.id: True}
    with signed_in_as(editor):
        seen = api.get(f"/api/approvals/{approval['id']}").json()
        refused = api.post(f"/api/approvals/{approval['id']}/approve", json={})
    assert seen["can_decide"] is False
    assert refused.status_code == 403
    assert "admin" in refused.json()["detail"]
    assert approval["can_decide"] is True

    decided = decide(api, run_service, script, approval, "approve", note="Within budget")
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"

    done = read(api, workflow, waiting["id"])
    assert done["status"] == "succeeded", done["error"]
    at = steps(done)
    assert at["approve"]["status"] == "succeeded"
    assert at["approve"]["output_json"] == {
        "approved": True,
        "note": "Within budget",
        "decided_by": {"id": user.id, "name": user.full_name},
        "status": "approved",
    }
    assert at["pay"]["status"] == "succeeded"
    assert at["tell"]["status"] == "skipped"
    # Its notification went with the decision, and it was audited.
    assert notified(db, approval["id"]) == {user.id: False}
    [entry] = entries(api, action="approval.decided")
    assert entry["target_kind"] == "approval"
    assert entry["details"]["decision"] == "approved"
    assert entry["details"]["kind"] == "workflow_step"
    # Decided once only.
    again = api.post(f"/api/approvals/{approval['id']}/reject", json={})
    assert again.status_code == 409


def test_a_rejection_takes_the_other_branch_with_the_note(api, store, run_service):
    workflow = spend_workflow(api)
    script = Script()
    waiting = start(api, workflow, script)
    approval = the_approval(api)

    resp = decide(api, run_service, script, approval, "reject", note="Too much")
    assert resp.status_code == 200, resp.text

    done = read(api, workflow, waiting["id"])
    assert done["status"] == "succeeded", done["error"]
    at = steps(done)
    assert at["approve"]["output_json"]["approved"] is False
    assert at["approve"]["output_json"]["status"] == "rejected"
    assert at["approve"]["output_json"]["note"] == "Too much"
    assert at["tell"]["status"] == "succeeded"
    assert at["pay"]["status"] == "skipped"


def test_a_low_risk_never_reaches_the_approval(api, store):
    workflow = spend_workflow(api)
    done = start(api, workflow, Script({"risk": "low", "amount": 5}))

    assert done["status"] == "succeeded", done["error"]
    assert steps(done)["approve"]["status"] == "skipped"
    assert api.get("/api/approvals", params={"kind": "workflow_step"}).json() == []


def test_an_editor_approval_is_decided_by_an_editor_and_notifies_editors_too(
    api, db, user, store, run_service, editor, participant
):
    workflow = spend_workflow(api, role="editor")
    script = Script()
    waiting = start(api, workflow, script)
    approval = the_approval(api)

    assert notified(db, approval["id"]) == {user.id: True, editor.id: True}
    with signed_in_as(participant):
        seen = api.get(f"/api/approvals/{approval['id']}").json()
        refused = api.post(f"/api/approvals/{approval['id']}/approve", json={})
    assert seen["can_decide"] is False
    assert refused.status_code == 403
    with signed_in_as(editor):
        resp = decide(api, run_service, script, approval, "approve")
    assert resp.status_code == 200, resp.text
    assert read(api, workflow, waiting["id"])["status"] == "succeeded"


def test_an_expired_approval_counts_as_not_approved(api, db, user, store, run_service):
    workflow = spend_workflow(api)
    script = Script()
    waiting = start(api, workflow, script)
    approval = the_approval(api)
    with store_session(store.id) as session:
        session.execute(
            update(SchemaChangeRequest)
            .where(SchemaChangeRequest.id == approval["id"])
            .values(expires_at=func.now() - timedelta(minutes=1))
        )
        session.commit()

    with use_model(model(script)):
        worker.expire_schema_change_requests_task.run()
        run_service.drain()

    done = read(api, workflow, waiting["id"])
    assert done["status"] == "succeeded", done["error"]
    at = steps(done)
    assert at["approve"]["output_json"] == {
        "approved": False,
        "note": None,
        "decided_by": None,
        "status": "expired",
    }
    assert at["tell"]["status"] == "succeeded"
    assert notified(db, approval["id"]) == {user.id: False}


def test_an_approval_expires_after_its_nodes_days_or_else_the_organisations(api, store):
    resp = api.put("/api/organisation/approval-expiry", json={"days": 3})
    assert resp.status_code == 200, resp.text
    agents = make_agents(api)
    own = start(api, spend_workflow(api, agents=agents, expiry_days=1), Script())
    by_setting = start(api, spend_workflow(api, agents=agents, name="Spend later"), Script())

    def days_left(run: dict) -> float:
        approval = api.get(f"/api/approvals/{steps(run)['approve']['approval_id']}").json()
        with store_session(store.id) as session:
            left = session.scalar(
                select(func.extract("epoch", SchemaChangeRequest.expires_at - func.now())).where(
                    SchemaChangeRequest.id == approval["id"]
                )
            )
        return float(left) / 86400

    assert 0.99 < days_left(own) <= 1
    assert 2.99 < days_left(by_setting) <= 3


def test_cancelling_the_run_cancels_its_approval_and_clears_its_notifications(api, db, user, store):
    workflow = spend_workflow(api)
    waiting = start(api, workflow, Script())
    approval = the_approval(api)
    assert notified(db, approval["id"]) == {user.id: True}

    resp = api.post(f"/api/workflows/{workflow['id']}/runs/{waiting['id']}/cancel")

    assert resp.status_code == 200, resp.text
    run = resp.json()
    assert run["status"] == "failed"
    assert run["error"] == f"Cancelled by {user.full_name}."
    assert api.get(f"/api/approvals/{approval['id']}").json()["status"] == "cancelled"
    assert notified(db, approval["id"]) == {user.id: False}
    refused = api.post(f"/api/approvals/{approval['id']}/approve", json={})
    assert refused.status_code == 409
    again = api.post(f"/api/workflows/{workflow['id']}/runs/{waiting['id']}/cancel")
    assert again.status_code == 409


# --- One list -----------------------------------------------------------------------------------


def test_the_list_holds_both_kinds_filtered_by_kind_and_status(api, store, run_service):
    request = schema_changes.propose(
        store.id,
        SchemaChange.model_validate({"ops": [CONTACTS]}),
        "For people",
        run_id=None,
        tool_call_id=None,
    )
    workflow = spend_workflow(api)
    script = Script()
    start(api, workflow, script)
    step = the_approval(api)
    decide(api, run_service, script, step, "reject")

    every = api.get("/api/approvals").json()
    assert {(a["kind"], a["status"]) for a in every} == {
        ("schema_change", "pending"),
        ("workflow_step", "rejected"),
    }
    [change] = api.get("/api/approvals", params={"kind": "schema_change"}).json()
    assert change["id"] == str(request.id)
    assert change["sql"] and change["approver_role"] == "editor" and change["step"] is None
    [rejected] = api.get("/api/approvals", params={"status": "rejected"}).json()
    assert rejected["id"] == step["id"]
    assert rejected["decided_by"]["name"]
    assert api.get("/api/approvals", params={"kind": "nope"}).status_code == 422
    # Impact is a schema change's only.
    assert api.get(f"/api/approvals/{step['id']}/impact").status_code == 422
    assert api.get(f"/api/approvals/{request.id}/impact").status_code == 200
    # The old routes are gone.
    assert api.get("/api/schema-changes").status_code == 404


def test_someone_outside_the_organisation_sees_no_approval(api, db, store, new_user):
    workflow = spend_workflow(api)
    start(api, workflow, Script())
    approval = the_approval(api)

    stranger = new_user()
    provisioned(db, store_of(stranger))
    with signed_in_as(stranger):
        assert api.get(f"/api/approvals/{approval['id']}").status_code == 404


def test_a_workflow_with_an_approval_node_exports_and_imports(api, store):
    workflow = spend_workflow(api, expiry_days=4)
    exported = api.get(f"/api/workflows/{workflow['id']}/export")
    assert exported.status_code == 200, exported.text
    nodes = {n["key"]: n for n in next(iter(exported.json()["workflows"].values()))["nodes"]}
    assert nodes["approve"]["kind"] == "approval"
    assert (nodes["approve"]["approver_role"], nodes["approve"]["expiry_days"]) == ("admin", 4)
