import json
import threading
from contextlib import contextmanager

import pytest
from conftest import user_store
from fastapi import HTTPException
from sqlalchemy import Engine, event, func
from sqlalchemy.exc import IntegrityError

from aios.api import workflows as workflows_api
from aios.api.provenance import built_by
from aios.api.skills import lock_live, unique_name
from aios.datastores.models import Agent, Workflow
from aios.models import WorkflowNodeKind
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

    # Archived, a workflow still reads, with its versions: past runs pin them, and a run's
    # subworkflow is opened from the version it ran. One nesting it still shows it as it is.
    assert api.post(f"/api/workflows/{inner['id']}/archive").status_code == 200
    assert api.get(f"/api/workflows/{inner['id']}").status_code == 200
    old = api.get(f"/api/workflows/{inner['id']}/versions/1").json()
    assert [n["key"] for n in old["nodes"]] == ["only"]
    node = api.get(f"/api/workflows/{outer['id']}").json()["current_version"]["nodes"][1]
    assert (node["name"], node["latest_version"]) == ("inner", 1)
    assert [w["name"] for w in api.get("/api/workflows").json()] == ["outer"]


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


def test_workflow_list_says_what_each_contains(api):
    """`contains` is what saving another workflow with a node running this one's current version
    would be checked against: the workflows whose versions it pins, at any depth."""
    agent = make_agent(api, "solo")
    a = make_workflow(api, "a", [agent_node("only", agent)])
    b = make_workflow(api, "b", [workflow_node("run_a", a)])
    c = make_workflow(api, "c", [workflow_node("run_b", b)])
    make_workflow(api, "d", [workflow_node("b", b), workflow_node("c", c)])

    def contains() -> dict[str, list[str]]:
        listed = api.get("/api/workflows").json()
        names = {w["id"]: w["name"] for w in listed}
        return {w["name"]: sorted(names[i] for i in w["contains"]) for w in listed}

    assert contains() == {"a": [], "b": ["a"], "c": ["a", "b"], "d": ["a", "b", "c"]}
    # b's new version runs only the agent, but c and d still pin its old one, which runs a.
    resp = api.put(f"/api/workflows/{b['id']}", json=workflow_body("b", [agent_node("x", agent)]))
    assert resp.status_code == 200
    assert contains() == {"a": [], "b": [], "c": ["a", "b"], "d": ["a", "b", "c"]}
    resp = api.put(f"/api/workflows/{a['id']}", json=workflow_body("a", [workflow_node("c", c)]))
    assert resp.status_code == 422


def test_restore_refuses_a_version_that_contains_its_workflow(api, db, user):
    """Versions saved before nesting was checked through other workflows can't be restored."""
    from aios.datastores.models import Workflow
    from aios.models import WorkflowNodeKind
    from aios.versioning import NodeSpec, save_workflow_version

    agent = make_agent(api, "solo")
    inner = make_workflow(api, "inner", [agent_node("only", agent)])
    outer = make_workflow(api, "outer", [workflow_node("run_inner", inner)])
    # inner v2 -> outer v1 -> inner v1, written directly as the old check would have allowed.
    with user_store(user) as store:
        save_workflow_version(
            store,
            store.get(Workflow, inner["id"]),
            nodes=[
                NodeSpec("run_outer", WorkflowNodeKind.workflow, outer["current_version"]["id"])
            ],
            edges=[],
            note=None,
            user_id=user.id,
        )
        store.commit()
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
    assert api.post(f"/api/workflows/{huge}/archive").status_code == 422
    assert api.post(f"/api/workflows/{workflow['id']}/versions/{huge}/restore").status_code == 422
    assert api.get(f"/api/workflows/{huge - 1}").status_code == 404


def test_invalid_workflows_are_rejected(api, db):
    agent = make_agent(api, "mine")
    try:
        cases = [
            # One the organisation's store doesn't have (another's ids are their store's own).
            ([{"key": "x", "kind": "agent", "version_id": 99_999}], [], "agent version"),
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
        pass


@contextmanager
def count_queries():
    """The statements run meanwhile, on any engine: the platform's and the stores'."""
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)


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
            "list": lambda: api.get("/api/workflows"),
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


def test_archived_targets_cant_be_saved_or_restored_into_a_workflow(api):
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
    outer_url = f"/api/workflows/{outer['id']}"
    resp = api.put(outer_url, json=workflow_body("Outer", [agent_node("w", writer)]))
    assert resp.json()["current_version"]["version"] == 2
    # Archiving is never refused, though the report's current version runs the researcher.
    assert api.post(f"/api/agents/{researcher['id']}/archive").status_code == 200
    assert api.post(f"/api/workflows/{report['id']}/archive").status_code == 200

    # History stays readable: the old nodes show what they ran, and archived things keep their
    # latest version.
    old = api.get(f"{outer_url}/versions/1").json()["nodes"]
    assert [(n["name"], n["version"], n["latest_version"]) for n in old] == [
        ("Researcher", 1, 1),
        ("Research report", 1, 1),
    ]
    assert [a["name"] for a in api.get("/api/agents").json()] == ["Writer"]
    assert [w["name"] for w in api.get("/api/workflows").json()] == ["Outer"]

    # But it can't be restored, nor can an archived agent's or workflow's versions be saved.
    resp = api.post(f"{outer_url}/versions/1/restore")
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "That version runs an agent and a workflow that has since been archived."
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

    # Their names stay reserved.
    resp = api.post("/api/agents", json={"name": "Researcher", "base_prompt": "p"})
    assert resp.status_code == 409
    resp = api.put(outer_url, json=workflow_body("Research report"))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "The organisation already has a workflow with that name."


def test_restoring_a_version_whose_targets_are_live_still_works(api):
    agent = make_agent(api, "Solo")
    other = make_agent(api, "Other")
    wf = make_workflow(api, "Flow", [agent_node("a", agent)])
    api.put(f"/api/workflows/{wf['id']}", json=workflow_body("Flow", [agent_node("b", other)]))
    resp = api.post(f"/api/workflows/{wf['id']}/versions/1/restore")
    assert resp.status_code == 200
    assert resp.json()["current_version"]["nodes"][0]["name"] == "Solo"
    # And now the agent is in use again.
    used = api.get(f"/api/agents/{agent['id']}/used-by").json()
    assert [w["name"] for w in used["workflows"]] == ["Flow"]


def test_unique_name_only_maps_the_name_index(user):
    """Any other integrity error, e.g. a pin of a version that doesn't exist, isn't a name clash."""
    with user_store(user) as store:
        workflow = Workflow(**built_by(user.id), name="Broken")
        store.add(workflow)
        index = workflows_api.WORKFLOW_NAME_INDEX
        with (
            pytest.raises(IntegrityError, match="agent_version_id"),
            unique_name(store, "a", index),
        ):
            save_workflow_version(
                store,
                workflow,
                nodes=[NodeSpec("x", WorkflowNodeKind.agent, 2**31 - 1)],
                edges=[],
                note=None,
                user_id=user.id,
            )
            store.commit()
        store.rollback()

        store.add(Workflow(**built_by(user.id), name="Taken"))
        store.commit()
        with pytest.raises(HTTPException) as caught, unique_name(store, "a workflow", index):
            store.add(Workflow(**built_by(user.id), name="Taken"))
            store.commit()
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
    specs, edges = workflows_api.resolve_graph(session, body, workflow)
    workflows_api.lock_new_graph(session, specs, workflow)
    return lambda: save_workflow_version(
        session, workflow, nodes=specs, edges=edges, note=None, user_id=user.id
    )


def archivable(api, kind: str) -> tuple[dict, dict]:
    """An agent, or a workflow, to archive; and a node that runs it."""
    agent = make_agent(api, "Racy")
    if kind == "agent":
        return agent, agent_node("t", agent)
    inner = make_workflow(api, "Inner", [agent_node("a", agent)])
    return inner, workflow_node("t", inner)


@pytest.mark.parametrize("kind", ["agent", "workflow"])
def test_archive_waits_for_an_in_flight_save_that_runs_it(api, user, kind):
    target, node = archivable(api, kind)
    flow = make_workflow(api, "Flow")
    url = f"/api/{kind}s/{target['id']}"

    with user_store(user) as session:
        save = held_save(session, user, flow["id"], [node])
        archiver, result = run_in_thread(lambda: api.post(f"{url}/archive"))
        assert archiver.is_alive(), "archive should block on the row lock"
        save()
        session.commit()

    archiver.join(timeout=10)
    # Never refused: the save went first, so the archive lists it as what uses the target.
    assert result["resp"].status_code == 200
    assert [w["name"] for w in api.get(f"{url}/used-by").json()["workflows"]] == ["Flow"]


@pytest.mark.parametrize("kind", ["agent", "workflow"])
def test_save_waits_for_an_in_flight_archive_of_what_it_runs(api, user, kind):
    target, node = archivable(api, kind)
    flow = make_workflow(api, "Flow")
    model = Agent if kind == "agent" else Workflow

    with user_store(user) as session:
        # An archive that has locked the row, but not committed yet.
        assert lock_live(session, model, [target["id"]])
        session.get(model, target["id"]).archived_at = func.now()
        saver, result = run_in_thread(
            lambda: api.put(f"/api/workflows/{flow['id']}", json=workflow_body("Flow", [node]))
        )
        assert saver.is_alive(), "save should block on the row lock"
        session.commit()

    saver.join(timeout=10)
    # A clean refusal, not a pin of an archived target.
    assert result["resp"].status_code == 422
    assert result["resp"].json()["detail"] == f"Unknown {kind} version"
    assert api.get(f"/api/workflows/{flow['id']}").json()["current_version"]["nodes"] == []


def test_saves_of_workflows_that_run_each_other_lock_in_one_order(api, user):
    """Each save locks every workflow it involves in id order, so they queue, not deadlock."""
    agent = make_agent(api, "Solo")
    first = make_workflow(api, "First", [agent_node("a", agent)])
    second = make_workflow(api, "Second", [agent_node("a", agent)])

    with user_store(user) as session:
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

    # Restoring a version without b drops b's position too.
    resp = api.put(
        f"/api/workflows/{wf['id']}",
        json=workflow_body("laid out", nodes, [("a", "b")], layout=layout),
    )
    assert (resp.json()["current_version"]["version"], resp.json()["layout"]) == (3, layout)
    resp = api.post(f"/api/workflows/{wf['id']}/versions/2/restore")
    assert resp.json()["current_version"]["version"] == 4
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


def test_a_save_without_a_layout_keeps_one_saved_while_it_waited(api, user):
    a = make_agent(api, "layout-race-a")
    b = make_agent(api, "layout-race-b")
    nodes = [agent_node("a", a), agent_node("b", b)]
    layout = {"a": {"x": 0, "y": 0}, "b": {"x": 1, "y": 1}}
    wf = make_workflow(api, "racy layout", nodes)
    api.put(f"/api/workflows/{wf['id']}", json=workflow_body("racy layout", nodes, layout=layout))

    with user_store(user) as session:
        # A save that has locked the workflow and moved node a, but not committed yet.
        assert lock_live(session, Workflow, [wf["id"]])
        session.get(Workflow, wf["id"]).layout = layout | {"a": {"x": 50, "y": 50}}
        body = workflow_body("racy layout", nodes[:1])
        saver, result = run_in_thread(lambda: api.put(f"/api/workflows/{wf['id']}", json=body))
        assert saver.is_alive(), "save should block on the row lock"
        session.commit()

    saver.join(timeout=10)
    # The waiting save prunes the layout it finds once locked, not the one it read before.
    assert result["resp"].json()["layout"] == {"a": {"x": 50, "y": 50}}


def test_a_save_based_on_an_older_version_is_refused(api):
    a = make_agent(api, "stale-a")
    b = make_agent(api, "stale-b")
    wf = make_workflow(api, "stale", [agent_node("a", a)])
    url = f"/api/workflows/{wf['id']}"
    # Another tab saves v2 while this one is still editing v1.
    resp = api.put(url, json=workflow_body("stale", [agent_node("b", b)], base_version=1))
    assert resp.json()["current_version"]["version"] == 2

    resp = api.put(url, json=workflow_body("stale", [agent_node("a", a)], base_version=1))
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "This workflow has changed since you started editing (now v2). Reload to see the changes."
    )
    assert [n["key"] for n in api.get(url).json()["current_version"]["nodes"]] == ["b"]

    # Without a base version the save goes ahead as before.
    resp = api.put(url, json=workflow_body("stale", [agent_node("a", a)]))
    assert resp.json()["current_version"]["version"] == 3


def test_a_stale_save_is_refused_as_stale_even_if_its_graph_is_now_invalid(api):
    a = make_agent(api, "stale-invalid")
    sub = make_workflow(api, "stale-sub", [agent_node("a", a)])
    wf = make_workflow(api, "stale-outer", [workflow_node("s", sub)])
    url = f"/api/workflows/{wf['id']}"
    # Another tab drops the subworkflow and archives it while this one is still editing v1.
    api.put(url, json=workflow_body("stale-outer", [agent_node("a", a)], base_version=1))
    assert api.post(f"/api/workflows/{sub['id']}/archive").status_code == 200

    nodes = [workflow_node("s", sub), agent_node("a", a)]
    resp = api.put(url, json=workflow_body("stale-outer", nodes, base_version=1))
    assert resp.status_code == 409
    assert "(now v2)" in resp.json()["detail"]


def test_the_base_version_is_checked_once_locked(api, user):
    agent = make_agent(api, "Solo")
    wf = make_workflow(api, "Flow", [agent_node("a", agent)])

    with user_store(user) as session:
        # A save of v2 that has locked the workflow, but not committed yet.
        save = held_save(session, user, wf["id"], [agent_node("b", agent)])
        body = workflow_body("Flow", [agent_node("c", agent)], base_version=1)
        saver, result = run_in_thread(lambda: api.put(f"/api/workflows/{wf['id']}", json=body))
        assert saver.is_alive(), "save should block on the row lock"
        save()
        session.commit()

    saver.join(timeout=10)
    assert result["resp"].status_code == 409
    assert "(now v2)" in result["resp"].json()["detail"]
