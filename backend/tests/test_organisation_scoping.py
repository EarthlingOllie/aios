"""Agents, skills, workflows and schedules belong to the organisation: every member sees, edits,
runs and deletes every one, names are unique within it, each says who made and last changed it,
and to anyone in another organisation they don't exist."""

from contextlib import nullcontext

import pytest
from conftest import delete_user, join_organisation, provisioned_user, user_store
from test_agents import agent_body, make_agent, make_skill
from test_chats import signed_in_as
from test_triggers import schedule
from test_workflow_runs import queued  # noqa: F401
from test_workflows import agent_node, make_workflow, workflow_body

from aios.datastores.models import Agent, Skill, Workflow
from aios.models import User


@pytest.fixture
def member(db, user):
    """A second member of `user`'s organisation. Gone first, with the organisation and what's in
    it: deleting a user alone leaves what they made, which is the organisation's."""
    u = join_organisation(db, user, "member", first_name="Member", last_name="Person")
    yield u
    delete_user(db, u)


@pytest.fixture
def outsider(db):
    """A user in an organisation of their own, with its store provisioned."""
    u = provisioned_user(db, "outsider")
    yield u
    delete_user(db, u)


def person(u: User) -> dict:
    return {"id": u.id, "name": u.full_name, "former_member": False}


def ok(resp, code: int = 200) -> dict:
    assert resp.status_code == code, resp.text
    return resp.json() if code != 204 else {}


def test_a_member_works_on_an_agent_another_member_made(api, user, member, db):
    agent = make_agent(api, "Reviewer", base_prompt="Review it.")
    url = f"/api/agents/{agent['id']}"
    with signed_in_as(member):
        assert [a["id"] for a in ok(api.get("/api/agents"))] == [agent["id"]]
        assert ok(api.get(url))["name"] == "Reviewer"
        changed = ok(api.put(url, json=agent_body(agent, base_prompt="Review it twice.")))
        assert changed["current_version"]["version"] == 2
        assert [v["version"] for v in ok(api.get(f"{url}/versions"))] == [2, 1]
        assert ok(api.get(f"{url}/versions/1"))["base_prompt"] == "Review it."
        restored = ok(api.post(f"{url}/versions/1/restore"))
        assert restored["current_version"]["version"] == 3
        run = ok(api.post(f"{url}/runs"), 201)
        assert [r["id"] for r in ok(api.get(f"{url}/runs"))] == [run["id"]]
        assert ok(api.get(f"{url}/runs/{run['id']}"))["created_by"] == person(member)
        assert ok(api.get(f"{url}/used-by"))["workflows"] == []
        ok(api.delete(f"{url}/runs/{run['id']}"), 204)
        ok(api.post(f"{url}/archive"))
    with user_store(user, "ro") as store:
        archived = store.get(Agent, agent["id"])
    assert archived.archived_at is not None and archived.archived_by == member.id


def test_a_member_works_on_a_skill_another_member_made(api, user, member):
    skill = make_skill(api, "Tone", content="Be kind.")
    url = f"/api/skills/{skill['id']}"
    with signed_in_as(member):
        assert [s["id"] for s in ok(api.get("/api/skills"))] == [skill["id"]]
        assert ok(api.get(url))["name"] == "Tone"
        body = {"name": "Tone", "content": "Be kinder."}
        assert ok(api.put(url, json=body))["current_version"]["version"] == 2
        assert [v["version"] for v in ok(api.get(f"{url}/versions"))] == [2, 1]
        assert ok(api.get(f"{url}/versions/1"))["content"] == "Be kind."
        assert ok(api.post(f"{url}/versions/1/restore"))["current_version"]["version"] == 3
        # An agent of the member's can use it.
        make_agent(api, "Kind", skills=[ok(api.get(url))])
    with signed_in_as(member):
        assert [a["agent_name"] for a in ok(api.get(f"{url}/used-by"))["agents"]] == ["Kind"]
        ok(api.post(f"{url}/archive"))
    with user_store(user, "ro") as store:
        assert store.get(Skill, skill["id"]).archived_by == member.id


def test_a_member_works_on_a_workflow_another_member_made(api, user, member, queued):  # noqa: F811
    agent = make_agent(api, "Step")
    workflow = make_workflow(api, "Pipeline", [agent_node("step", agent)])
    url = f"/api/workflows/{workflow['id']}"
    with signed_in_as(member):
        assert [w["id"] for w in ok(api.get("/api/workflows"))] == [workflow["id"]]
        assert ok(api.get(url))["name"] == "Pipeline"
        # Another member's agent in it, alongside the first's.
        theirs = make_agent(api, "Their step")
        body = workflow_body("Pipeline", [agent_node("step", agent), agent_node("two", theirs)])
        assert ok(api.put(url, json=body))["current_version"]["version"] == 2
        assert [v["version"] for v in ok(api.get(f"{url}/versions"))] == [2, 1]
        assert len(ok(api.get(f"{url}/versions/1"))["nodes"]) == 1
        assert ok(api.post(f"{url}/versions/1/restore"))["current_version"]["version"] == 3
        run = ok(api.post(f"{url}/runs", json={"input": "go"}), 201)
        assert queued == [run["id"]]
        assert [r["id"] for r in ok(api.get(f"{url}/runs"))] == [run["id"]]
        assert ok(api.get(f"{url}/runs/{run['id']}"))["created_by"] == person(member)
        assert ok(api.get(f"{url}/used-by"))["workflows"] == []
        ok(api.post(f"{url}/archive"))
    with user_store(user, "ro") as store:
        assert store.get(Workflow, workflow["id"]).archived_by == member.id


def test_a_member_works_on_a_schedule_another_member_made(api, user, member):
    agent = make_agent(api, "Daily")
    made = ok(api.post("/api/triggers", json=schedule("agent", agent["id"])), 201)
    url = f"/api/triggers/{made['id']}"
    with signed_in_as(member):
        assert [t["id"] for t in ok(api.get("/api/triggers"))] == [made["id"]]
        assert ok(api.get(url))["target_name"] == "Daily"
        changed = ok(api.put(url, json=schedule("agent", agent["id"], cron="0 10 * * *")))
        assert changed["cron"] == "0 10 * * *"
        assert (changed["created_by"], changed["updated_by"]) == (person(user), person(member))
        # One for a workflow of the first member's.
        workflow = make_workflow(api, "Flow")
        ok(api.post("/api/triggers", json=schedule("workflow", workflow["id"])), 201)
        ok(api.delete(url), 204)


def test_another_organisation_cant_reach_any_of_it(api, outsider, queued):  # noqa: F811
    skill = make_skill(api, "Tone")
    agent = make_agent(api, "Reviewer", skills=[skill])
    workflow = make_workflow(api, "Pipeline", [agent_node("step", agent)])
    trigger = ok(api.post("/api/triggers", json=schedule("agent", agent["id"])), 201)
    agent_run = ok(api.post(f"/api/agents/{agent['id']}/runs"), 201)
    workflow_run = ok(api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"}), 201)
    a, s, w, t = (
        f"/api/agents/{agent['id']}",
        f"/api/skills/{skill['id']}",
        f"/api/workflows/{workflow['id']}",
        f"/api/triggers/{trigger['id']}",
    )
    with signed_in_as(outsider):
        for path in ("/api/agents", "/api/skills", "/api/workflows", "/api/triggers"):
            assert ok(api.get(path)) == []
        reads = [
            *(a, f"{a}/versions", f"{a}/versions/1", f"{a}/runs", f"{a}/used-by"),
            f"{a}/runs/{agent_run['id']}",
            *(s, f"{s}/versions", f"{s}/versions/1", f"{s}/used-by"),
            *(w, f"{w}/versions", f"{w}/versions/1", f"{w}/runs", f"{w}/used-by"),
            f"{w}/runs/{workflow_run['id']}",
            t,
        ]
        for path in reads:
            assert api.get(path).status_code == 404, path
        writes = [
            api.put(a, json=agent_body(agent, name="Mine now")),
            api.put(s, json={"name": "Mine now"}),
            api.put(w, json=workflow_body("Mine now")),
            api.put(t, json=schedule("agent", agent["id"])),
            api.post(f"{a}/versions/1/restore"),
            api.post(f"{s}/versions/1/restore"),
            api.post(f"{w}/versions/1/restore"),
            api.post(f"{a}/runs"),
            api.post(f"{w}/runs", json={"input": "x"}),
            api.delete(f"{a}/runs/{agent_run['id']}"),
            api.delete(t),
            *(api.post(f"{x}/{action}") for x in (a, s, w) for action in ("archive", "unarchive")),
        ]
        assert [r.status_code for r in writes] == [404] * len(writes)
        # Nor can they build on it: the targets are unknown to them.
        unknown = [
            api.post(
                "/api/agents",
                json={"name": "x", "skill_version_ids": [skill["current_version"]["id"]]},
            ),
            api.post("/api/workflows", json=workflow_body("x", [agent_node("a", agent)])),
            api.post("/api/triggers", json=schedule("agent", agent["id"])),
            api.post("/api/triggers", json=schedule("workflow", workflow["id"])),
        ]
        assert [r.status_code for r in unknown] == [422] * len(unknown)
    assert queued == [workflow_run["id"]]


def test_ids_repeat_across_organisations_and_each_reaches_only_its_own(
    api,
    outsider,
    queued,  # noqa: F811
):
    """Each organisation's agents, skills and workflows are in its own store, whose ids are its
    own: two organisations' first agent, skill and workflow have the same ids. Every route, with
    those ids, reaches the asking organisation's, never the other's."""
    built = {}
    for who in ("user", "outsider"):
        with signed_in_as(outsider) if who == "outsider" else nullcontext():
            skill = make_skill(api, f"{who} tone", content=f"{who} content")
            agent = make_agent(api, f"{who} agent", base_prompt=f"{who} prompt", skills=[skill])
            workflow = make_workflow(api, f"{who} flow", [agent_node("step", agent)])
            built[who] = (skill, agent, workflow)
    mine, theirs = built["user"], built["outsider"]
    # The same ids in both stores, version ids included.
    for a, b in zip(mine, theirs, strict=True):
        assert (a["id"], a["current_version"]["id"]) == (b["id"], b["current_version"]["id"])

    def everything(who: str) -> list:
        skill, agent, workflow = built[who]
        s, a, w = (
            f"/api/skills/{skill['id']}",
            f"/api/agents/{agent['id']}",
            f"/api/workflows/{workflow['id']}",
        )
        return [
            [x["name"] for x in ok(api.get("/api/skills"))],
            [x["name"] for x in ok(api.get("/api/agents"))],
            [x["name"] for x in ok(api.get("/api/workflows"))],
            ok(api.get(s))["name"],
            ok(api.get(f"{s}/versions/1"))["content"],
            [x["agent_name"] for x in ok(api.get(f"{s}/used-by"))["agents"]],
            ok(api.get(a))["name"],
            ok(api.get(f"{a}/versions/1"))["base_prompt"],
            [x["name"] for x in ok(api.get(f"{a}/versions/1"))["skills"]],
            [x["name"] for x in ok(api.get(f"{a}/used-by"))["workflows"]],
            ok(api.get(w))["name"],
            [n["name"] for n in ok(api.get(f"{w}/versions/1"))["nodes"]],
        ]

    def expected(who: str) -> list:
        return [
            [f"{who} tone"],
            [f"{who} agent"],
            [f"{who} flow"],
            f"{who} tone",
            f"{who} content",
            [f"{who} agent"],
            f"{who} agent",
            f"{who} prompt",
            [f"{who} tone"],
            [f"{who} flow"],
            f"{who} flow",
            [f"{who} agent"],
        ]

    assert everything("user") == expected("user")
    with signed_in_as(outsider):
        assert everything("outsider") == expected("outsider")
        # Changing theirs leaves ours alone.
        _, agent, workflow = theirs
        ok(api.post(f"/api/agents/{agent['id']}/archive"))
        ok(api.put(f"/api/workflows/{workflow['id']}", json=workflow_body("outsider flow")))
    _, agent, workflow = mine
    assert ok(api.get(f"/api/agents/{agent['id']}"))["archived_at"] is None
    assert ok(api.get(f"/api/workflows/{workflow['id']}"))["current_version"]["version"] == 1
    # A run of ours is found by us, by its agent's id, and not by them with the same id.
    run = ok(api.post(f"/api/agents/{agent['id']}/runs"), 201)
    assert ok(api.get(f"/api/agents/{agent['id']}/runs/{run['id']}"))["agent_name"] == "user agent"
    with signed_in_as(outsider):
        assert api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").status_code == 404
        assert ok(api.get(f"/api/agents/{agent['id']}/runs")) == []
        assert ok(api.get(f"/api/agents/{agent['id']}/versions"))[0]["run_count"] == 0
    assert ok(api.get(f"/api/agents/{agent['id']}/versions"))[0]["run_count"] == 1


def test_names_are_unique_within_the_organisation(api, member, outsider):
    make_agent(api, "Reviewer")
    make_skill(api, "Tone")
    make_workflow(api, "Pipeline")
    clashes = {
        "/api/agents": {"name": "Reviewer"},
        "/api/skills": {"name": "Tone"},
        "/api/workflows": workflow_body("Pipeline"),
    }
    with signed_in_as(member):
        for path, body in clashes.items():
            resp = api.post(path, json=body)
            assert resp.status_code == 409, path
            assert "organisation already has" in resp.json()["detail"]
    with signed_in_as(outsider):
        for path, body in clashes.items():
            assert api.post(path, json=body).status_code == 201, path


def test_each_says_who_made_and_last_changed_it(api, user, member):
    agent = make_agent(api, "Reviewer")
    skill = make_skill(api, "Tone")
    workflow = make_workflow(api, "Pipeline")
    trigger = ok(api.post("/api/triggers", json=schedule("agent", agent["id"])), 201)
    me = person(user)
    for made in (agent, skill, workflow, trigger):
        assert (made["created_by"], made["updated_by"]) == (me, me)
    with signed_in_as(member):
        ok(api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, base_prompt="Changed")))
        ok(api.put(f"/api/skills/{skill['id']}", json={"name": "Tone", "content": "Changed"}))
        ok(api.put(f"/api/workflows/{workflow['id']}", json=workflow_body("Pipeline")))
    them = person(member)
    for path in ("agents", "skills", "workflows"):
        [listed] = ok(api.get(f"/api/{path}"))
        assert (listed["created_by"], listed["updated_by"]) == (me, them), path
    [first, second] = reversed(ok(api.get(f"/api/agents/{agent['id']}/versions")))
    assert (first["created_by"], second["created_by"]) == (me, them)
    assert ok(api.get(f"/api/agents/{agent['id']}/versions/2"))["created_by"] == them
    assert ok(api.get(f"/api/skills/{skill['id']}/versions"))[0]["created_by"] == them
    assert ok(api.get(f"/api/workflows/{workflow['id']}/versions"))[0]["created_by"] == me
    [listed] = ok(api.get("/api/triggers"))
    assert (listed["created_by"], listed["updated_by"]) == (me, me)


def test_archiving_an_agent_finds_the_workflows_and_schedules_any_member_made(api, member):
    agent = make_agent(api, "Builder")
    with signed_in_as(member):
        workflow = make_workflow(api, "Builds", [agent_node("build", agent)])
        daily = ok(api.post("/api/triggers", json=schedule("workflow", workflow["id"])), 201)
        own = ok(api.post("/api/triggers", json=schedule("agent", agent["id"])), 201)
    found = ok(api.get(f"/api/agents/{agent['id']}/used-by"))
    [listed] = found["workflows"]
    assert listed["id"] == workflow["id"]
    assert [t["id"] for t in listed["triggers"]] == [daily["id"]]
    ok(api.post(f"/api/agents/{agent['id']}/archive"))
    with signed_in_as(member):
        paused = ok(api.get(f"/api/triggers/{own['id']}"))
    # Paused by whoever archived the agent.
    assert (paused["enabled"], paused["updated_by"]["name"]) == (False, "Test User")
