"""Mentioning a workflow in a chat starts a run of it, followed by a card and posted back."""

import asyncio

import pytest
from conftest import store_of
from sqlalchemy import func, select, update
from test_chats import add, make_chat, messages, signed_in_as
from test_organisation_scoping import member  # noqa: F401
from test_workflow_runs import Queued, abandon, agents, echo
from test_workflows import agent_node, make_workflow

from aios import invocations, workflows
from aios.api import chats
from aios.api import workflows as workflows_api
from aios.datastores.models import ChatMessage, Invocation
from aios.testing.fake_model import model, use_model
from aios.tools import TOOLS
from aios.workflows import execute_run


@pytest.fixture
def queued(monkeypatch, user) -> Queued:
    """The runs mentions start, instead of sending them to the worker."""
    ids = Queued(store_of(user))
    monkeypatch.setattr(
        workflows_api.run_workflow_task, "delay", lambda store_id, run_id: ids.append(run_id)
    )
    return ids


def mention(workflow: dict) -> dict:
    return {"kind": "workflow", "id": workflow["id"]}


def say(api, chat: dict, body: str, *mentioned: dict) -> dict:
    """Post a message mentioning the workflows; returns the response (`{messages, invocations}`)."""
    resp = api.post(
        f"/api/chats/{chat['id']}/messages",
        json={"body": body, "mentions": [mention(w) for w in mentioned]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def echo_agent(api, name: str) -> dict:
    return agents(api, name)[name]


def test_mentioning_a_workflow_posts_its_acknowledgement_and_card_at_once(api, user, queued):
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)

    posted = say(api, chat, f"@[Digest](workflow:{wf['id']}) sum it up", wf)

    message, card = posted["messages"]
    assert (message["kind"], message["author"]["kind"]) == ("text", "user")
    assert message["invocation_id"] is None
    assert (card["kind"], card["author"]["kind"], card["author"]["name"]) == (
        "run_card",
        "workflow",
        "Digest",
    )
    [invocation] = posted["invocations"]
    assert card["invocation_id"] == invocation["id"]
    assert invocation["message_id"] == message["id"]
    assert invocation["requested_by"] == user.id
    assert (invocation["target_kind"], invocation["target_id"]) == ("workflow", wf["id"])
    # The response is from before the run starts, which happens after it.
    assert invocation["status"] == "queued"
    assert invocation["workflow_run_id"] is None and invocation["workflow_run"] is None
    # And the chat shows them together, the acknowledgement now the handoff's (`briefs`).
    assert messages(api, chat)["messages"] == [message, {**card, "body": "On it."}]


def test_the_run_starts_with_the_message_and_the_poll_follows_it_to_its_result(api, queued):
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)
    message, card = say(api, chat, f"@[Digest](workflow:{wf['id']}) sum it up", wf)["messages"]

    # Started just after the response, with the message (a mention as `@name`) as its input.
    [run_id] = queued
    live = messages(api, chat, after=card["id"])
    assert live["messages"] == []
    [invocation] = live["invocations"]
    assert (invocation["status"], invocation["workflow_run_id"]) == ("running", run_id)
    assert invocation["started_at"] and not invocation["finished_at"]
    run = invocation["workflow_run"]
    assert (run["id"], run["status"], run["input"]) == (run_id, "queued", "@Digest sum it up")
    # The card draws its graph.
    assert [n["key"] for n in run["version"]["nodes"]] == ["one"]
    assert run["steps"] == []

    with use_model(echo()):
        asyncio.run(execute_run(queued.store, run_id))

    # The result is posted back as a message from the workflow, with the invocation's end.
    done = messages(api, chat, after=card["id"])
    [result] = done["messages"]
    assert (result["kind"], result["author"]["kind"], result["author"]["name"]) == (
        "text",
        "workflow",
        "Digest",
    )
    assert result["body"] == "first(@Digest sum it up)"
    assert result["invocation_id"] == invocation["id"]
    [finished] = done["invocations"]
    assert (finished["id"], finished["status"], finished["error"]) == (
        invocation["id"],
        "succeeded",
        None,
    )
    assert finished["finished_at"]
    assert finished["workflow_run"]["status"] == "succeeded"
    assert [s["status"] for s in finished["workflow_run"]["steps"]] == ["succeeded"]

    # Nothing's live any more, so the poll has nothing to report.
    assert messages(api, chat, after=result["id"]) == {"messages": [], "invocations": []}
    # The run is the workflow's, marked as started from the chat.
    [listed] = api.get(f"/api/workflows/{wf['id']}/runs").json()
    assert (listed["id"], listed["chat_id"], listed["chat_name"]) == (run_id, chat["id"], "Design")


def results(api, chat: dict) -> list[tuple[str, str]]:
    """The chat's messages from its workflows that aren't cards: who posted each, and what."""
    return [
        (m["author"]["name"], m["body"])
        for m in messages(api, chat)["messages"]
        if m["author"] and m["author"]["kind"] == "workflow" and m["kind"] == "text"
    ]


def test_a_start_thats_refused_fails_the_invocation_with_why(api, queued, monkeypatch):
    searcher = echo_agent(api, "searcher")
    resp = api.put(
        f"/api/agents/{searcher['id']}",
        json={"name": "searcher", "tools": [{"key": "web_search", "config": {}}]},
    )
    assert resp.status_code == 200, resp.text
    empty = make_workflow(api, "Empty")
    search = make_workflow(api, "Search", [agent_node("s", resp.json())])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", empty)
    add(api, chat, "workflow", search)
    # The agent's tool leaves the library, so its version can't run.
    monkeypatch.delitem(TOOLS, "web_search")

    posted = say(api, chat, "Go", empty, search)
    # Each mentioned workflow gets a card.
    assert [m["author"]["name"] for m in posted["messages"][1:]] == ["Empty", "Search"]
    assert queued == []
    by_id = {i["id"]: i for i in messages(api, chat)["invocations"]}
    empty_card, search_card = posted["messages"][1:]
    refused = by_id[empty_card["invocation_id"]]
    assert (refused["status"], refused["error"]) == ("failed", "This workflow has no nodes")
    assert refused["workflow_run"] is None and refused["run_deleted"] is False
    assert refused["started_at"] is None and refused["finished_at"]
    assert by_id[search_card["invocation_id"]]["status"] == "failed"
    assert "web_search" in by_id[search_card["invocation_id"]]["error"]
    # Both are posted at once, so in either order.
    posted_by = dict(results(api, chat))
    assert posted_by["Empty"] == "Couldn't start the run: This workflow has no nodes"
    assert posted_by["Search"].startswith("Couldn't start the run: ")


def test_a_run_that_cant_be_queued_fails_the_invocation(api, monkeypatch):
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)

    def broker_down(store_id, run_id):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(workflows_api.run_workflow_task, "delay", broker_down)
    say(api, chat, "Go", wf)
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["error"]) == (
        "failed",
        "Could not queue the run; please try again shortly.",
    )
    assert invocation["workflow_run_id"] is None and invocation["run_deleted"] is False
    # No run is left behind.
    assert api.get(f"/api/workflows/{wf['id']}/runs").json() == []


def test_several_workflows_mentioned_each_start(api, queued):
    one = make_workflow(api, "One", [agent_node("a", echo_agent(api, "first"))])
    two = make_workflow(api, "Two", [agent_node("b", echo_agent(api, "second"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", one)
    add(api, chat, "workflow", two)

    posted = say(api, chat, "Both of you", one, two)
    assert len(posted["invocations"]) == 2 and len(queued) == 2
    with use_model(echo()):
        for run_id in queued:
            asyncio.run(execute_run(queued.store, run_id))
    assert sorted(results(api, chat)) == [
        ("One", "first(Both of you)"),
        ("Two", "second(Both of you)"),
    ]
    assert {i["status"] for i in messages(api, chat)["invocations"]} == {"succeeded"}


def test_mentions_in_a_workflows_result_start_nothing(api, queued):
    loop = make_workflow(api, "Loop", [agent_node("a", echo_agent(api, "looper"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", loop)
    say(api, chat, "Start", loop)

    async def script(messages, info):
        yield f"Again, @[Loop](workflow:{loop['id']})"

    with use_model(model(script)):
        asyncio.run(execute_run(queued.store, queued[0]))
    assert results(api, chat) == [("Loop", f"Again, @[Loop](workflow:{loop['id']})")]
    # One run, one invocation: the result's mention started nothing.
    assert len(queued) == 1
    assert len(messages(api, chat)["invocations"]) == 1


def test_a_failed_run_posts_its_error(api, queued):
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)
    say(api, chat, "Go", wf)
    with use_model(echo(fail={"first"})):
        asyncio.run(execute_run(queued.store, queued[0]))
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["error"]) == ("failed", "one: first broke")
    assert results(api, chat) == [("Digest", "The run failed: one: first broke")]


def test_a_card_whose_run_was_deleted_says_so(api, queued):
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)
    say(api, chat, "Finished", wf)
    say(api, chat, "Never ran", wf)
    finished, unstarted = queued
    with use_model(echo()):
        asyncio.run(execute_run(queued.store, finished))
    for run_id in queued:
        resp = api.delete(f"/api/workflows/{wf['id']}/runs/{run_id}")
        assert resp.status_code == 204, resp.text

    first, second = messages(api, chat)["invocations"]
    # The card keeps how it went, and says the run has gone.
    assert (first["status"], first["run_deleted"], first["workflow_run"]) == (
        "succeeded",
        True,
        None,
    )
    # A run deleted before it finished fails its card, which says why.
    assert (second["status"], second["error"], second["run_deleted"]) == (
        "failed",
        "The run was deleted before it finished.",
        True,
    )
    assert results(api, chat)[-1] == (
        "Digest",
        "The run failed: The run was deleted before it finished.",
    )


def test_a_run_whose_worker_died_fails_its_card_when_the_chat_is_read(api, queued):
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)
    say(api, chat, "Go", wf)
    abandon(queued.store, queued[0])
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["error"]) == ("failed", workflows.WORKER_STOPPED)
    assert results(api, chat) == [("Digest", f"The run failed: {workflows.WORKER_STOPPED}")]


def test_an_invocation_whose_start_was_lost_fails_when_the_chat_is_read(
    api, in_store, queued, monkeypatch
):
    async def lost(store_id, invocation_ids):
        """The process stops between posting the message and starting the run."""

    monkeypatch.setattr(chats, "start_invocations", lost)
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)
    [made] = say(api, chat, "Go", wf)["invocations"]
    # Not yet: it may simply not have started yet.
    assert messages(api, chat)["invocations"][0]["status"] == "queued"
    in_store.execute(
        update(Invocation)
        .where(Invocation.id == made["id"])
        .values(created_at=func.now() - invocations.START_WITHIN)
    )
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["error"]) == ("failed", invocations.NEVER_STARTED)
    assert queued == []


def test_deleting_a_chat_deletes_its_messages_and_invocations_but_not_the_runs(
    api, in_store, queued
):
    wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", wf)
    say(api, chat, "Go", wf)
    with use_model(echo()):
        asyncio.run(execute_run(queued.store, queued[0]))
    assert api.delete(f"/api/chats/{chat['id']}").status_code == 204
    assert in_store.scalar(select(func.count()).where(ChatMessage.chat_id == chat["id"])) == 0
    assert in_store.scalar(select(func.count()).where(Invocation.chat_id == chat["id"])) == 0
    [run] = api.get(f"/api/workflows/{wf['id']}/runs").json()
    assert (run["id"], run["status"], run["chat_id"]) == (queued[0], "succeeded", None)


def test_a_run_going_in_a_private_chat_deleted_as_its_owner_leaves_goes_on_without_it(
    api,
    in_store,
    user,
    member,  # noqa: F811
    queued,
):
    """A private chat its owner leaves with nobody else in it is deleted
    (`organisations.hand_over_chats`) as its owner deleting it is: with its messages and
    invocations, and a run one started, not yet finished, goes on and finishes without it."""
    with signed_in_as(member):
        wf = make_workflow(api, "Digest", [agent_node("one", echo_agent(api, "first"))])
        chat = make_chat(api, "Mine", visibility="private")
        add(api, chat, "workflow", wf)
        say(api, chat, "Go", wf)
        assert api.post("/api/organisation/leave").status_code == 200
    assert in_store.scalar(select(func.count()).where(ChatMessage.chat_id == chat["id"])) == 0
    assert in_store.scalar(select(func.count()).where(Invocation.chat_id == chat["id"])) == 0

    with use_model(echo()):
        asyncio.run(execute_run(queued.store, queued[0]))
    [run] = api.get(f"/api/workflows/{wf['id']}/runs").json()
    assert (run["id"], run["status"], run["chat_id"]) == (queued[0], "succeeded", None)
    assert in_store.scalar(select(func.count()).where(ChatMessage.chat_id == chat["id"])) == 0
