import json
import threading
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func
from sqlalchemy.exc import IntegrityError

from aios.api import workflows as workflows_api
from aios.api.skills import lock_live, unique_name
from aios.db import SessionLocal, engine
from aios.models import Agent, Workflow, WorkflowNodeKind
from aios.versioning import NodeSpec, save_workflow_version
from aios.workflows import GraphError, validate_graph


def make_agent(api, name: str, base_prompt: str = "") -> dict:
    resp = api.post("/api/agents", json={"name": name, "base_prompt": base_prompt})
    assert resp.status_code == 201, resp.text
    return resp.json()


def agent_node(key: str, agent: dict) -> dict:
    return {"key": key, "kind": "agent", "version_id": agent["current_version"]["id"]}


def workflow_node(key: str, workflow: dict) -> dict:
    return {"key": key, "kind": "workflow", "version_id": workflow["current_version"]["id"]}


def make_workflow(api, name: str, nodes: list[dict] = (), edges: list[tuple] = ()) -> dict:
    resp = api.post("/api/workflows", json=workflow_body(name, nodes, edges))
    assert resp.status_code == 201, resp.text
    return resp.json()


def workflow_body(name: str, nodes=(), edges=(), **extra) -> dict:
    return {
        "name": name,
        "nodes": list(nodes),
        "edges": [{"source": s, "target": t} for s, t in edges],
        **extra,
    }


def test_validate_graph():
    validate_graph(["a", "b", "c"], [("a", "b"), ("a", "c"), ("b", "c")])
    with pytest.raises(GraphError, match="only once"):
        validate_graph(["a", "a"], [])
    with pytest.raises(GraphError, match="Invalid node key"):
        validate_graph(["a b"], [])
    validate_graph(["a" * 64], [])
    # `$` would also match before a trailing newline, letting these through.
    for key in ["", "step\n", "a" * 64 + "\n", "a" * 65]:
        with pytest.raises(GraphError, match="Invalid node key"):
            validate_graph([key], [])
    with pytest.raises(GraphError, match="unknown node"):
        validate_graph(["a"], [("a", "b")])
    with pytest.raises(GraphError, match="itself"):
        validate_graph(["a"], [("a", "a")])
    with pytest.raises(GraphError, match="cycle: b → c → b$"):
        validate_graph(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "b")])


def test_validate_graph_reports_only_nodes_on_a_cycle() -> None:
    # d is downstream of the cycle, so it never runs out of incoming edges, but isn't on it.
    with pytest.raises(GraphError, match="cycle: b → c → b$"):
        validate_graph(["a", "b", "c", "d"], [("b", "c"), ("c", "b"), ("c", "d")])
    with pytest.raises(GraphError, match="cycle: c → b → c$"):
        validate_graph(["d", "c", "b"], [("b", "c"), ("c", "b"), ("c", "d")])
    # Two disjoint cycles: one concrete cycle is named, not a mix of both.
    with pytest.raises(GraphError, match="cycle: a → b → a$"):
        validate_graph(["a", "b", "x", "y"], [("a", "b"), ("b", "a"), ("x", "y"), ("y", "x")])
    with pytest.raises(GraphError, match="cycle: a → b → c → a$"):
        validate_graph(["a", "b", "c", "e"], [("a", "b"), ("b", "c"), ("c", "a"), ("e", "a")])


def test_workflow_graphs_agents_and_is_versioned(api):
    research = make_agent(api, "researcher")
    writer = make_agent(api, "writer")
    nodes = [agent_node("research", research), agent_node("write", writer)]
    wf = make_workflow(api, "report", nodes, [("research", "write")])
    v1 = wf["current_version"]
    assert v1["version"] == 1
    assert [(n["key"], n["name"], n["version"]) for n in v1["nodes"]] == [
        ("research", "researcher", 1),
        ("write", "writer", 1),
    ]
    assert v1["edges"] == [{"source": "research", "target": "write"}]

    # Renaming and re-saving the same graph doesn't create a version.
    same = workflow_body("report v2", nodes, [("research", "write")])
    resp = api.put(f"/api/workflows/{wf['id']}", json=same)
    assert resp.json()["current_version"]["version"] == 1
    assert resp.json()["name"] == "report v2"

    # The pinned agent version stays put when the agent changes, and the newer one is reported.
    writer_v2 = api.put(
        f"/api/agents/{writer['id']}", json={"name": "writer", "base_prompt": "Be terse."}
    ).json()
    node = api.get(f"/api/workflows/{wf['id']}").json()["current_version"]["nodes"][1]
    assert (node["version"], node["latest_version"]) == (1, 2)

    # Taking it is a change to the graph, so a new version.
    nodes[1] = agent_node("write", writer_v2)
    resp = api.put(
        f"/api/workflows/{wf['id']}",
        json=workflow_body("report v2", nodes, [("research", "write")], note="newer writer"),
    )
    v2 = resp.json()["current_version"]
    assert (v2["version"], v2["note"], v2["nodes"][1]["version"]) == (2, "newer writer", 2)

    history = api.get(f"/api/workflows/{wf['id']}/versions").json()
    assert [(v["version"], v["node_count"]) for v in history] == [(2, 2), (1, 2)]
    old = api.get(f"/api/workflows/{wf['id']}/versions/1").json()
    assert old["nodes"][1]["version"] == 1

    restored = api.post(f"/api/workflows/{wf['id']}/versions/1/restore").json()
    v3 = restored["current_version"]
    assert (v3["version"], v3["note"]) == (3, "Restored from v1")
    assert v3["nodes"][1]["version_id"] == writer["current_version"]["id"]
    assert v3["edges"] == v1["edges"]

    listed = api.get("/api/workflows").json()
    assert [(w["name"], w["version"], w["node_count"]) for w in listed] == [("report v2", 3, 2)]


def test_reordering_nodes_or_edges_is_a_no_op_save(api):
    a, b, c = (make_agent(api, name) for name in ("a", "b", "c"))
    nodes = [agent_node("a", a), agent_node("b", b), agent_node("c", c)]
    edges = [("a", "b"), ("a", "c"), ("b", "c")]
    inner = make_workflow(api, "inner", nodes, edges)
    outer = make_workflow(api, "outer", [workflow_node("sub", inner)])

    def save(nodes, edges) -> dict:
        resp = api.put(f"/api/workflows/{inner['id']}", json=workflow_body("inner", nodes, edges))
        assert resp.status_code == 200, resp.text
        return resp.json()["current_version"]

    for reordered in (
        save(nodes[::-1], edges),
        save(nodes, edges[::-1]),
        save([nodes[1], nodes[2], nodes[0]], [edges[2], edges[0], edges[1]]),
    ):
        # Same version, still shown in the order it was first saved in.
        assert reordered["version"] == 1
        assert [n["key"] for n in reordered["nodes"]] == ["a", "b", "c"]
        assert [(e["source"], e["target"]) for e in reordered["edges"]] == edges

    # So a workflow nesting it isn't told there's a newer version.
    node = api.get(f"/api/workflows/{outer['id']}").json()["current_version"]["nodes"][0]
    assert (node["version"], node["latest_version"]) == (1, 1)

    # An actual change to the edges still creates one.
    assert save(nodes, edges[:2])["version"] == 2


def test_workflows_nest_as_subgraphs(api):
    agent = make_agent(api, "solo")
    inner = make_workflow(api, "inner", [agent_node("only", agent)])
    outer = make_workflow(
        api,
        "outer",
        [agent_node("first", agent), workflow_node("then", inner)],
        [("first", "then")],
    )
    node = outer["current_version"]["nodes"][1]
    assert (node["kind"], node["name"], node["target_id"]) == ("workflow", "inner", inner["id"])

    # A workflow can't contain itself, at any version.
    resp = api.put(
        f"/api/workflows/{inner['id']}",
        json=workflow_body("inner", [workflow_node("self", inner)]),
    )
    assert resp.status_code == 422
    assert "itself" in resp.json()["detail"]

    # Nor can a workflow or agent that a live workflow's current version runs be deleted.
    resp = api.delete(f"/api/workflows/{inner['id']}")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Remove this workflow from these workflows first: outer."
    resp = api.delete(f"/api/agents/{agent['id']}")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Remove this agent from these workflows first: inner, outer."

    # A deleted workflow no longer counts as running anything, though its versions still pin
    # them. Deleting it first frees inner, and then inner frees the agent.
    assert api.delete(f"/api/workflows/{outer['id']}").status_code == 204
    resp = api.delete(f"/api/agents/{agent['id']}")
    assert resp.json()["detail"] == "Remove this agent from these workflows first: inner."
    assert api.delete(f"/api/workflows/{inner['id']}").status_code == 204
    assert api.delete(f"/api/agents/{agent['id']}").status_code == 204
    assert api.get("/api/workflows").json() == []
    for url in (f"/api/workflows/{outer['id']}", f"/api/workflows/{outer['id']}/versions/1"):
        assert api.get(url).status_code == 404
    assert api.delete(f"/api/workflows/{outer['id']}").status_code == 404


def test_a_workflow_cant_contain_itself_through_other_workflows(api):
    agent = make_agent(api, "solo")
    a = make_workflow(api, "a", [agent_node("only", agent)])
    b = make_workflow(api, "b", [workflow_node("run_a", a)])
    c = make_workflow(api, "c", [workflow_node("run_b", b)])
    # Pinning workflows that don't lead back is fine, even reaching one along two paths.
    d = make_workflow(api, "d", [workflow_node("b", b), workflow_node("c", c)])

    # a v2 -> b v1 -> a v1, and a v2 -> d v1 -> c v1 -> b v1 -> a v1: both refused, naming the
    # workflows a was pinned through.
    for nodes, through in [
        ([workflow_node("run_b", b)], "b"),
        ([workflow_node("run_c", c)], "c"),
        ([agent_node("first", agent), workflow_node("run_d", d)], "d"),
        ([workflow_node("run_b", b), workflow_node("run_d", d)], "b, d"),
    ]:
        resp = api.put(f"/api/workflows/{a['id']}", json=workflow_body("a", nodes))
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "even through another workflow" in detail
        assert detail.endswith(f"(via {through})")
    assert api.get(f"/api/workflows/{a['id']}").json()["current_version"]["version"] == 1

    # Taking a newer version of a workflow that doesn't contain a is still fine.
    resp = api.put(f"/api/workflows/{b['id']}", json=workflow_body("b", [agent_node("x", agent)]))
    assert resp.status_code == 200
    resp = api.put(
        f"/api/workflows/{a['id']}", json=workflow_body("a", [workflow_node("run_b", resp.json())])
    )
    assert resp.status_code == 200, resp.text


def test_restore_refuses_a_version_that_contains_its_workflow(api, db, user):
    """Versions saved before nesting was checked through other workflows can't be restored."""
    from aios.models import Workflow, WorkflowNodeKind
    from aios.versioning import NodeSpec, save_workflow_version

    agent = make_agent(api, "solo")
    inner = make_workflow(api, "inner", [agent_node("only", agent)])
    outer = make_workflow(api, "outer", [workflow_node("run_inner", inner)])
    # inner v2 -> outer v1 -> inner v1, written directly as the old check would have allowed.
    save_workflow_version(
        db,
        db.get(Workflow, inner["id"]),
        nodes=[NodeSpec("run_outer", WorkflowNodeKind.workflow, outer["current_version"]["id"])],
        edges=[],
        note=None,
        user_id=user.id,
    )
    db.commit()
    resp = api.put(
        f"/api/workflows/{inner['id']}", json=workflow_body("inner", [agent_node("x", agent)])
    )
    assert resp.json()["current_version"]["version"] == 3

    resp = api.post(f"/api/workflows/{inner['id']}/versions/2/restore")
    assert resp.status_code == 422
    assert "even through another workflow (via outer)" in resp.json()["detail"]
    # Restoring a version that doesn't contain it still works.
    resp = api.post(f"/api/workflows/{inner['id']}/versions/1/restore")
    assert resp.json()["current_version"]["version"] == 4


def test_ids_beyond_the_integer_columns_are_invalid_not_server_errors(api):
    huge = 2**31  # one past Postgres INTEGER
    workflow = make_workflow(api, "bounded")
    for kind in ("agent", "workflow"):
        nodes = [{"key": "a", "kind": kind, "version_id": huge}]
        assert api.post("/api/workflows", json=workflow_body("huge", nodes)).status_code == 422
        url = f"/api/workflows/{workflow['id']}"
        assert api.put(url, json=workflow_body("bounded", nodes)).status_code == 422
    for url in [
        f"/api/workflows/{huge}",
        f"/api/workflows/{huge}/versions",
        f"/api/workflows/{workflow['id']}/versions/{huge}",
    ]:
        assert api.get(url).status_code == 422, url
    assert api.delete(f"/api/workflows/{huge}").status_code == 422
    assert api.post(f"/api/workflows/{workflow['id']}/versions/{huge}/restore").status_code == 422
    assert api.get(f"/api/workflows/{huge - 1}").status_code == 404


def test_invalid_workflows_are_rejected(api, db):
    from aios.models import Agent, User
    from aios.versioning import save_agent_version

    agent = make_agent(api, "mine")
    other = User(github_id=-2, login="someone-else")
    db.add(other)
    db.flush()
    theirs = Agent(owner_id=other.id, name="theirs")
    db.add(theirs)
    their_version = save_agent_version(
        db, theirs, base_prompt="", skills=[], note=None, user_id=other.id
    )
    db.commit()
    try:
        cases = [
            ([{"key": "x", "kind": "agent", "version_id": their_version.id}], [], "agent version"),
            ([{"key": "x", "kind": "workflow", "version_id": 0}], [], "workflow version"),
            ([agent_node("a", agent), agent_node("b", agent)], [("a", "b"), ("b", "a")], "cycle"),
            ([agent_node("a", agent)], [("a", "missing")], "unknown node"),
            ([agent_node("a" * 64 + "\n", agent)], [], "Invalid node key"),
        ]
        for nodes, edges, message in cases:
            resp = api.post("/api/workflows", json=workflow_body("bad", nodes, edges))
            assert resp.status_code == 422, (message, resp.text)
            assert message in resp.json()["detail"]
        make_workflow(api, "dup")
        assert api.post("/api/workflows", json=workflow_body("dup")).status_code == 409
    finally:
        db.delete(other)
        db.commit()


@contextmanager
def count_queries():
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_workflow_responses_take_a_fixed_number_of_queries(api):
    """Loading a graph for a response doesn't cost queries per node."""
    agents = [make_agent(api, f"agent {i}") for i in range(15)]
    subs = [make_workflow(api, f"sub {i}") for i in range(15)]

    def query_counts(n: int) -> dict[str, int]:
        nodes = [agent_node(f"a{i}", a) for i, a in enumerate(agents[: n // 2])]
        nodes += [workflow_node(f"w{i}", w) for i, w in enumerate(subs[: n - n // 2])]
        with count_queries() as create:
            wf = api.post("/api/workflows", json=workflow_body(f"wf {n}", nodes)).json()["id"]
        requests = {
            "update": lambda: api.put(
                f"/api/workflows/{wf}", json=workflow_body(f"wf {n}", nodes[:-1])
            ),
            "get": lambda: api.get(f"/api/workflows/{wf}"),
            "version": lambda: api.get(f"/api/workflows/{wf}/versions/1"),
            "restore": lambda: api.post(f"/api/workflows/{wf}/versions/1/restore"),
        }
        counts = {"create": len(create)}
        for name, request in requests.items():
            with count_queries() as statements:
                assert request().status_code == 200
            counts[name] = len(statements)
        return counts

    assert query_counts(3) == query_counts(30)


def test_only_current_versions_block_deletion(api):
    researcher = make_agent(api, "Researcher")
    writer = make_agent(api, "Writer")
    report = make_workflow(
        api,
        "Research report",
        [agent_node("research", researcher), agent_node("write", writer)],
        [("research", "write")],
    )
    outer = make_workflow(
        api,
        "Outer",
        [agent_node("first", researcher), workflow_node("then", report)],
        [("first", "then")],
    )
    resp = api.delete(f"/api/agents/{researcher['id']}")
    assert resp.status_code == 409
    assert resp.json()["detail"].endswith(": Outer, Research report.")

    # Take the researcher out of both workflows' current versions, and the report out of
    # Outer's: only their history uses them now.
    report_url, outer_url = f"/api/workflows/{report['id']}", f"/api/workflows/{outer['id']}"
    resp = api.put(report_url, json=workflow_body("Research report", [agent_node("w", writer)]))
    assert resp.json()["current_version"]["version"] == 2
    resp = api.put(outer_url, json=workflow_body("Outer", [agent_node("w", writer)]))
    assert resp.json()["current_version"]["version"] == 2
    assert api.delete(f"/api/agents/{researcher['id']}").status_code == 204
    assert api.delete(report_url).status_code == 204

    # History stays readable: the old nodes show what they ran, with no latest version.
    old = api.get(f"{outer_url}/versions/1").json()["nodes"]
    assert [(n["name"], n["version"], n["latest_version"]) for n in old] == [
        ("Researcher", 1, None),
        ("Research report", 1, None),
    ]
    assert api.get(f"/api/agents/{researcher['id']}").status_code == 404
    assert [a["name"] for a in api.get("/api/agents").json()] == ["Writer"]
    assert [w["name"] for w in api.get("/api/workflows").json()] == ["Outer"]

    # But it can't be restored, nor can a deleted agent's or workflow's versions be saved.
    resp = api.post(f"{outer_url}/versions/1/restore")
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "That version runs an agent and a workflow that has since been deleted."
    )
    assert api.get(outer_url).json()["current_version"]["version"] == 2
    for node, message in (
        (agent_node("r", researcher), "Unknown agent version"),
        (workflow_node("r", report), "Unknown workflow version"),
    ):
        resp = api.put(outer_url, json=workflow_body("Outer", [node]))
        assert resp.status_code == 422
        assert resp.json()["detail"] == message
        resp = api.post("/api/workflows", json=workflow_body("New", [node]))
        assert resp.status_code == 422
        assert resp.json()["detail"] == message

    # The names are free again, but only among live rows: a live name still clashes.
    make_agent(api, "Researcher")
    make_workflow(api, "Research report")
    resp = api.put(outer_url, json=workflow_body("Research report"))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "You already have a workflow with that name."


def test_restoring_a_version_whose_targets_are_live_still_works(api):
    agent = make_agent(api, "Solo")
    other = make_agent(api, "Other")
    wf = make_workflow(api, "Flow", [agent_node("a", agent)])
    api.put(f"/api/workflows/{wf['id']}", json=workflow_body("Flow", [agent_node("b", other)]))
    resp = api.post(f"/api/workflows/{wf['id']}/versions/1/restore")
    assert resp.status_code == 200
    assert resp.json()["current_version"]["nodes"][0]["name"] == "Solo"
    # And now the agent is in use again.
    assert api.delete(f"/api/agents/{agent['id']}").status_code == 409


def test_unique_name_only_maps_the_name_index(db, user):
    """Any other integrity error, e.g. a pin of a version that doesn't exist, isn't a name clash."""
    workflow = Workflow(owner_id=user.id, name="Broken")
    db.add(workflow)
    index = workflows_api.WORKFLOW_NAME_INDEX
    with pytest.raises(IntegrityError, match="agent_version_id"), unique_name(db, "a", index):
        save_workflow_version(
            db,
            workflow,
            nodes=[NodeSpec("x", WorkflowNodeKind.agent, 2**31 - 1)],
            edges=[],
            note=None,
            user_id=user.id,
        )
        db.commit()  # the deferred foreign key fails here
    db.rollback()

    db.add(Workflow(owner_id=user.id, name="Taken"))
    db.commit()
    with pytest.raises(HTTPException) as caught, unique_name(db, "a workflow", index):
        db.add(Workflow(owner_id=user.id, name="Taken"))
        db.commit()
    assert caught.value.status_code == 409


def run_in_thread(call) -> tuple[threading.Thread, dict]:
    result = {}
    thread = threading.Thread(target=lambda: result.update(resp=call()))
    thread.start()
    thread.join(timeout=0.5)
    return thread, result


def held_save(session, user, workflow_id: int, nodes: list[dict]):
    """A workflow save that has checked and locked its graph, but not committed yet."""
    workflow = session.get(Workflow, workflow_id)
    body = workflows_api.WorkflowIn(**workflow_body(workflow.name, nodes))
    specs, edges = workflows_api.resolve_graph(session, user.id, body, workflow)
    workflows_api.lock_new_graph(session, specs, workflow)
    return lambda: save_workflow_version(
        session, workflow, nodes=specs, edges=edges, note=None, user_id=user.id
    )


def deletable(api, kind: str) -> tuple[dict, dict]:
    """An agent, or a workflow, to delete; and a node that runs it."""
    agent = make_agent(api, "Racy")
    if kind == "agent":
        return agent, agent_node("t", agent)
    inner = make_workflow(api, "Inner", [agent_node("a", agent)])
    return inner, workflow_node("t", inner)


@pytest.mark.parametrize("kind", ["agent", "workflow"])
def test_delete_waits_for_an_in_flight_save_that_runs_it(api, user, kind):
    target, node = deletable(api, kind)
    flow = make_workflow(api, "Flow")
    url = f"/api/{kind}s/{target['id']}"

    with SessionLocal() as session:
        save = held_save(session, user, flow["id"], [node])
        deleter, result = run_in_thread(lambda: api.delete(url))
        assert deleter.is_alive(), "delete should block on the row lock"
        save()
        session.commit()

    deleter.join(timeout=10)
    # The delete saw the save once it got the lock, so the workflow never runs a deleted target.
    assert result["resp"].status_code == 409
    assert result["resp"].json()["detail"].endswith(": Flow.")
    assert api.get(url).status_code == 200


@pytest.mark.parametrize("kind", ["agent", "workflow"])
def test_save_waits_for_an_in_flight_delete_of_what_it_runs(api, kind):
    target, node = deletable(api, kind)
    flow = make_workflow(api, "Flow")
    model = Agent if kind == "agent" else Workflow

    with SessionLocal() as session:
        # A delete that has locked the row and found it unused, but not committed yet.
        assert lock_live(session, model, [target["id"]])
        session.get(model, target["id"]).deleted_at = func.now()
        saver, result = run_in_thread(
            lambda: api.put(f"/api/workflows/{flow['id']}", json=workflow_body("Flow", [node]))
        )
        assert saver.is_alive(), "save should block on the row lock"
        session.commit()

    saver.join(timeout=10)
    # A clean refusal, not a foreign key error at commit or a pin of a deleted target.
    assert result["resp"].status_code == 422
    assert result["resp"].json()["detail"] == f"Unknown {kind} version"
    assert api.get(f"/api/workflows/{flow['id']}").json()["current_version"]["nodes"] == []


def test_saves_of_workflows_that_run_each_other_lock_in_one_order(api, user):
    """Each save locks every workflow it involves in id order, so they queue, not deadlock."""
    agent = make_agent(api, "Solo")
    first = make_workflow(api, "First", [agent_node("a", agent)])
    second = make_workflow(api, "Second", [agent_node("a", agent)])

    with SessionLocal() as session:
        # Saving Second to run First locks both workflows, First (the lower id) first.
        save = held_save(session, user, second["id"], [workflow_node("f", first)])
        body = workflow_body("First", [agent_node("b", agent)])
        saver, result = run_in_thread(lambda: api.put(f"/api/workflows/{first['id']}", json=body))
        assert saver.is_alive(), "the other save should queue behind this one"
        save()
        session.commit()

    saver.join(timeout=10)
    assert result["resp"].status_code == 200


def test_layout_is_saved_with_the_workflow_but_not_versioned(api):
    a = make_agent(api, "layout-a")
    b = make_agent(api, "layout-b")
    nodes = [agent_node("a", a), agent_node("b", b)]
    layout = {"a": {"x": 0, "y": 0}, "b": {"x": 120.5, "y": -40}}
    wf = make_workflow(api, "laid out", nodes, [("a", "b")])
    assert wf["layout"] == {}

    resp = api.put(
        f"/api/workflows/{wf['id']}",
        json=workflow_body("laid out", nodes, [("a", "b")], layout=layout),
    )
    assert resp.status_code == 200, resp.text
    # Moving nodes is like renaming: it changes the workflow, not its graph.
    assert resp.json()["current_version"]["version"] == 1
    assert resp.json()["layout"] == layout
    assert api.get(f"/api/workflows/{wf['id']}").json()["layout"] == layout

    # Leaving the layout out keeps it, but positions of nodes no longer in the graph are dropped,
    # whether they were saved before or sent now.
    resp = api.put(
        f"/api/workflows/{wf['id']}", json=workflow_body("laid out", nodes, [("a", "b")])
    )
    assert resp.json()["layout"] == layout
    resp = api.put(f"/api/workflows/{wf['id']}", json=workflow_body("laid out", nodes[:1]))
    assert resp.json()["current_version"]["version"] == 2
    assert resp.json()["layout"] == {"a": {"x": 0, "y": 0}}
    resp = api.put(
        f"/api/workflows/{wf['id']}",
        json=workflow_body("laid out", nodes[:1], layout=layout | {"gone": {"x": 1, "y": 1}}),
    )
    assert resp.json()["layout"] == {"a": {"x": 0, "y": 0}}


def test_layout_on_create_and_invalid_positions(api):
    a = make_agent(api, "layout-c")
    resp = api.post(
        "/api/workflows",
        json=workflow_body("created", [agent_node("a", a)], layout={"a": {"x": 5, "y": 6}}),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["layout"] == {"a": {"x": 5, "y": 6}}
    body = json.dumps(workflow_body("bad", [agent_node("a", a)], layout={"a": {"x": 0, "y": 0}}))
    # 1e309 is JSON for a number that only fits a float as infinity.
    for bad in ['{"x": 1}', '{"x": "inf", "y": 0}', '{"x": 1e309, "y": 0}']:
        resp = api.post(
            "/api/workflows",
            content=body.replace('{"x": 0, "y": 0}', bad),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, bad


def test_agent_summaries_carry_the_current_version_id(api):
    agent = make_agent(api, "pinnable")
    [summary] = [a for a in api.get("/api/agents").json() if a["id"] == agent["id"]]
    assert summary["version_id"] == agent["current_version"]["id"]
