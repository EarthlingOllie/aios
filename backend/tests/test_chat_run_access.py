"""A run started from a chat opens for any member of that chat, read-only; not for anyone else.

The chat's member here is a user of another organisation, whom the owner can no longer add
(chats are the organisation's), so they're seated in the chat directly (`seat`), as a membership
from before that would be: what's left of the rule is what these tests pin, until runs are read
by organisation alone."""

import asyncio

import pytest
from conftest import delete_user, sign_up
from fake_model import replies, use_model
from test_chat_invocations import echo_agent, say
from test_chats import add, make_chat, messages, signed_in_as
from test_triggers import make_agent
from test_workflow_runs import echo
from test_workflows import agent_node, make_workflow, workflow_node

from aios.api import workflows as workflows_api
from aios.models import ChatMember, ChatMemberKind, ChatMemberRole, User
from aios.workflows import execute_run


@pytest.fixture
def started(monkeypatch) -> list[int]:
    """The ids of the workflow runs mentions start, instead of sending them to the worker."""
    ids = []
    monkeypatch.setattr(workflows_api.run_workflow_task, "delay", ids.append)
    return ids


def signed_up(db, github_id: int, login: str):
    """A user in a personal organisation of their own, for as long as the fixture lasts."""
    u = sign_up(db, github_id, login)
    yield u
    delete_user(db, u)


@pytest.fixture
def member(db):
    """A second user, whom the first adds to their chats."""
    yield from signed_up(db, -2, "member")


@pytest.fixture
def outsider(db):
    """A third user, in none of the chats."""
    yield from signed_up(db, -4, "outsider")


def seat(db, chat: dict, member: User) -> None:
    """Put `member`, a user of another organisation, in the chat."""
    db.add(
        ChatMember(
            chat_id=chat["id"],
            kind=ChatMemberKind.user,
            user_id=member.id,
            role=ChatMemberRole.member,
        )
    )
    db.commit()


def run_workflow_from_chat(api, db, started, member) -> tuple[dict, dict, int]:
    """The owner's workflow, run from a chat `member` is a member of: (workflow, chat, run id)."""
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)
    seat(db, chat, member)
    say(api, chat, "Go", wf)
    [run_id] = started
    with use_model(echo()):
        asyncio.run(execute_run(run_id))
    return wf, chat, run_id


def test_a_member_reads_another_owners_workflow_run_and_its_nodes_conversation(
    api, db, started, member, outsider
):
    wf, chat, run_id = run_workflow_from_chat(api, db, started, member)
    url = f"/api/workflows/{wf['id']}/runs/{run_id}"
    owners = api.get(url).json()
    assert owners["is_owner"] is True

    with signed_in_as(member):
        resp = api.get(url)
        assert resp.status_code == 200, resp.text
        run = resp.json()
        # The same run its owner sees, but theirs to read only.
        assert run == {**owners, "is_owner": False}
        assert (run["workflow_name"], run["chat_id"], run["status"]) == (
            "Digest",
            chat["id"],
            "succeeded",
        )
        [step] = run["steps"]
        conversation = api.get(f"/api/agents/{step['agent_id']}/runs/{step['agent_run_id']}")
        assert conversation.status_code == 200, conversation.text
        assert (conversation.json()["agent_name"], conversation.json()["is_owner"]) == (
            "first",
            False,
        )
        assert conversation.json()["ui_messages"]

    with signed_in_as(outsider):
        assert api.get(url).status_code == 404
        assert (
            api.get(f"/api/agents/{step['agent_id']}/runs/{step['agent_run_id']}").status_code
            == 404
        )


def run_agent_from_chat(api, db, member) -> tuple[dict, dict, int]:
    """The owner's agent, run from a chat `member` is a member of: (agent, chat, run id)."""
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Bot's chat")
    add(api, chat, "agent", agent)
    seat(db, chat, member)
    with use_model(replies("Hi.")):
        resp = api.post(
            f"/api/chats/{chat['id']}/messages",
            json={"body": "Go", "mentions": [{"kind": "agent", "id": agent["id"]}]},
        )
        assert resp.status_code == 201, resp.text
    [invocation] = messages(api, chat)["invocations"]
    return agent, chat, invocation["agent_run_id"]


def test_a_member_reads_another_owners_agent_run_but_no_other_run_of_it(api, db, member, outsider):
    agent, chat, run_id = run_agent_from_chat(api, db, member)
    url = f"/api/agents/{agent['id']}/runs/{run_id}"
    owners = api.get(url).json()
    assert owners["is_owner"] is True
    # A run of the same agent that no chat started.
    resp = api.post(f"/api/agents/{agent['id']}/runs")
    assert resp.status_code == 201, resp.text
    unshared = resp.json()["id"]

    with signed_in_as(member):
        resp = api.get(url)
        assert resp.status_code == 200, resp.text
        run = resp.json()
        assert run == {**owners, "is_owner": False}
        assert (run["agent_name"], run["chat_id"], run["status"]) == (
            "Bot",
            chat["id"],
            "succeeded",
        )
        assert api.get(f"/api/agents/{agent['id']}/runs/{unshared}").status_code == 404
        # Not under another agent's id either.
        mine = make_agent(api, "Mine")
        assert api.get(f"/api/agents/{mine['id']}/runs/{run_id}").status_code == 404

    with signed_in_as(outsider):
        assert api.get(url).status_code == 404


def test_everything_but_reading_the_run_stays_the_owners(api, db, started, member):
    wf, _, wf_run = run_workflow_from_chat(api, db, started, member)
    agent, _, agent_run = run_agent_from_chat(api, db, member)
    a, w = f"/api/agents/{agent['id']}", f"/api/workflows/{wf['id']}"

    with signed_in_as(member):
        refused = [
            api.get(a),
            api.put(a, json={"name": "Mine now"}),
            api.delete(a),
            api.get(f"{a}/runs"),
            api.post(f"{a}/runs"),
            api.get(f"{a}/versions"),
            api.delete(f"{a}/runs/{agent_run}"),
            api.post(
                f"{a}/runs/{agent_run}/chat",
                json={
                    "messages": [
                        {"id": "m", "role": "user", "parts": [{"type": "text", "text": "Hi"}]}
                    ]
                },
            ),
            api.get(w),
            api.put(w, json={"name": "Mine now", "nodes": [], "edges": []}),
            api.delete(w),
            api.get(f"{w}/runs"),
            api.post(f"{w}/runs", json={"input": "x"}),
            api.get(f"{w}/versions"),
            api.delete(f"{w}/runs/{wf_run}"),
        ]
        assert [r.status_code for r in refused] == [404] * len(refused)

    # All still there, unchanged.
    assert api.get(a).json()["name"] == "Bot"
    assert api.get(w).json()["name"] == "Digest"
    assert api.get(f"{a}/runs/{agent_run}").status_code == 200
    assert api.get(f"{w}/runs/{wf_run}").status_code == 200
    assert started == [wf_run]


def test_the_rule_lapses_when_the_chat_is_deleted(api, db, started, member):
    wf, wf_chat, wf_run = run_workflow_from_chat(api, db, started, member)
    agent, agent_chat, agent_run = run_agent_from_chat(api, db, member)
    [step] = api.get(f"/api/workflows/{wf['id']}/runs/{wf_run}").json()["steps"]
    urls = [
        f"/api/workflows/{wf['id']}/runs/{wf_run}",
        f"/api/agents/{step['agent_id']}/runs/{step['agent_run_id']}",
        f"/api/agents/{agent['id']}/runs/{agent_run}",
    ]
    with signed_in_as(member):
        assert [api.get(u).status_code for u in urls] == [200, 200, 200]

    for chat in (wf_chat, agent_chat):
        assert api.delete(f"/api/chats/{chat['id']}").status_code == 204

    with signed_in_as(member):
        assert [api.get(u).status_code for u in urls] == [404, 404, 404]
    # The runs stay their owner's.
    assert [api.get(u).status_code for u in urls] == [200, 200, 200]


def test_the_rule_lapses_for_a_member_removed_from_the_chat(api, db, member):
    agent, chat, run_id = run_agent_from_chat(api, db, member)
    url = f"/api/agents/{agent['id']}/runs/{run_id}"
    [row] = [
        m
        for m in api.get(f"/api/chats/{chat['id']}").json()["members"]["users"]
        if m["target_id"] == member.id
    ]
    assert api.delete(f"/api/chats/{chat['id']}/members/{row['id']}").status_code == 204
    with signed_in_as(member):
        assert api.get(url).status_code == 404


def test_a_deleted_run_or_workflow_is_gone_for_members_too(api, db, started, member):
    wf, _, wf_run = run_workflow_from_chat(api, db, started, member)
    agent, _, agent_run = run_agent_from_chat(api, db, member)
    [step] = api.get(f"/api/workflows/{wf['id']}/runs/{wf_run}").json()["steps"]
    conversation = f"/api/agents/{step['agent_id']}/runs/{step['agent_run_id']}"

    assert api.delete(f"/api/agents/{agent['id']}/runs/{agent_run}").status_code == 204
    # A deleted workflow's runs outlive it, but no one reads them through its pages.
    assert api.delete(f"/api/workflows/{wf['id']}").status_code == 204
    with signed_in_as(member):
        assert api.get(f"/api/agents/{agent['id']}/runs/{agent_run}").status_code == 404
        assert api.get(f"/api/workflows/{wf['id']}/runs/{wf_run}").status_code == 404
        assert api.get(conversation).status_code == 404


def test_a_member_opens_a_subworkflow_of_the_run_and_its_nodes_conversations(
    api, db, started, member, outsider
):
    a = echo_agent(api, "deep")
    inner = make_workflow(api, "Inner", [agent_node("leaf", a)])
    middle = make_workflow(api, "Middle", [workflow_node("in", inner)])
    outer = make_workflow(api, "Outer", [workflow_node("mid", middle)])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", outer)
    seat(db, chat, member)
    say(api, chat, "Go", outer)
    [run_id] = started
    with use_model(echo()):
        asyncio.run(execute_run(run_id))
    run_url = f"/api/workflows/{outer['id']}/runs/{run_id}"

    def subworkflow(*path: str):
        return api.get(f"{run_url}/subworkflow", params={"path": list(path)})

    # The versions the run pinned, as its page opens them level by level.
    assert subworkflow("mid").json()["id"] == middle["current_version"]["id"]
    with signed_in_as(member):
        mid = subworkflow("mid")
        assert mid.status_code == 200, mid.text
        assert [n["key"] for n in mid.json()["nodes"]] == ["in"]
        deepest = subworkflow("mid", "in").json()
        assert (deepest["id"], [n["key"] for n in deepest["nodes"]]) == (
            inner["current_version"]["id"],
            ["leaf"],
        )
        # Only along the run's graph: not an agent node, nor a key it doesn't have.
        assert subworkflow("mid", "in", "leaf").status_code == 404
        assert subworkflow("nope").status_code == 404
        # And the conversation of an agent node however deep it is.
        leaf = next(
            s for s in api.get(run_url).json()["steps"] if s["path"] == ["mid", "in", "leaf"]
        )
        resp = api.get(f"/api/agents/{leaf['agent_id']}/runs/{leaf['agent_run_id']}")
        assert resp.status_code == 200, resp.text

    with signed_in_as(outsider):
        assert subworkflow("mid").status_code == 404
