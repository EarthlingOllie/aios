import pytest

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
    with pytest.raises(GraphError, match="unknown node"):
        validate_graph(["a"], [("a", "b")])
    with pytest.raises(GraphError, match="itself"):
        validate_graph(["a"], [("a", "a")])
    with pytest.raises(GraphError, match="cycle through: b, c"):
        validate_graph(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "b")])


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

    # Nor can a workflow or agent still run by another workflow be deleted.
    resp = api.delete(f"/api/workflows/{inner['id']}")
    assert resp.status_code == 409
    assert "outer" in resp.json()["detail"]
    resp = api.delete(f"/api/agents/{agent['id']}")
    assert resp.status_code == 409
    assert "inner" in resp.json()["detail"] and "outer" in resp.json()["detail"]

    assert api.delete(f"/api/workflows/{outer['id']}").status_code == 204
    assert api.delete(f"/api/workflows/{inner['id']}").status_code == 204
    assert api.delete(f"/api/agents/{agent['id']}").status_code == 204


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
        assert detail.endswith(f": {through} runs it")
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
    assert "even through another workflow: outer runs it" in resp.json()["detail"]
    # Restoring a version that doesn't contain it still works.
    resp = api.post(f"/api/workflows/{inner['id']}/versions/1/restore")
    assert resp.json()["current_version"]["version"] == 4


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
