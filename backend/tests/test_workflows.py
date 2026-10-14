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
