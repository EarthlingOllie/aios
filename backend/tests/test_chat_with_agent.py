"""**Chat with this agent**: chat creation with one agent and no name makes a new open chat that
includes only that agent (not the default chat agents), with the caller as its first person,
named "<Agent> – <person> – <date>"; a clash adds " (2)", " (3)" and so on. It requires
permission to invoke the agent.

Through the API, as a signed-in member, with the scripted model."""

from datetime import UTC, datetime

import pytest
from conftest import join_organisation
from test_agents import agent_body, make_agent
from test_chat_agent_invocations import replies_from
from test_chats import make_chat, signed_in_as
from test_permissions import PARTICIPANT, set_role

from aios.testing.fake_model import replies, use_model


def today() -> str:
    return datetime.now(UTC).date().isoformat()


def chat_with(api, agent: dict):
    return api.post("/api/chats", json={"agent_id": agent["id"]})


@pytest.fixture
def participant(db, user):
    u = join_organisation(db, user, "participant", first_name="Pat", last_name="Participant")
    set_role(db, u, PARTICIPANT, of=user)
    return u


def test_chat_with_an_agent_names_it_and_includes_only_that_agent(api, user):
    agent = make_agent(api, "Researcher")

    resp = chat_with(api, agent)

    assert resp.status_code == 201, resp.text
    chat = resp.json()
    assert chat["name"] == f"Researcher – {user.full_name} – {today()}"
    assert [m["target_id"] for m in chat["members"]["users"]] == [user.id]
    # That agent, and not the default chat agents every other new chat gets.
    assert [m["target_id"] for m in chat["members"]["agents"]] == [agent["id"]]
    assert chat["members"]["workflows"] == []
    assert chat["is_member"] is True


def test_each_click_makes_a_new_chat_with_the_next_free_suffix(api, user):
    agent = make_agent(api, "Researcher")
    base = f"Researcher – {user.full_name} – {today()}"

    names = [chat_with(api, agent).json()["name"] for _ in range(3)]

    assert names == [base, f"{base} (2)", f"{base} (3)"]


def test_a_renamed_chat_frees_its_name(api, user):
    agent = make_agent(api, "Researcher")
    base = f"Researcher – {user.full_name} – {today()}"
    first = chat_with(api, agent).json()
    chat_with(api, agent)
    resp = api.put(f"/api/chats/{first['id']}", json={"name": "Literature review"})
    assert resp.status_code == 200, resp.text

    assert chat_with(api, agent).json()["name"] == base


def test_a_name_given_is_kept(api):
    agent = make_agent(api, "Researcher")
    resp = api.post("/api/chats", json={"agent_id": agent["id"], "name": "Reading list"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "Reading list"


def test_a_chat_needs_a_name_or_an_agent(api):
    assert api.post("/api/chats", json={}).status_code == 422
    assert api.post("/api/chats", json={"name": " "}).status_code == 422


def test_anyone_in_the_organisation_reads_it_and_its_members_rename_it(api, user, participant):
    agent = make_agent(api, "Researcher")
    chat = chat_with(api, agent).json()
    url = f"/api/chats/{chat['id']}"

    with signed_in_as(participant):
        listed = {c["id"]: c for c in api.get("/api/chats").json()}
        assert listed[chat["id"]]["is_member"] is False
        assert api.get(url).status_code == 200
        # Not a member yet: they can't rename it.
        assert api.put(url, json={"name": "Mine now"}).status_code == 403
        assert api.post(f"{url}/join").status_code == 200
        resp = api.put(url, json={"name": "Ours"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Ours"
    resp = api.put(url, json={"name": "Back to me"})
    assert resp.status_code == 200, resp.text


def test_its_agent_replies_when_mentioned(api, user):
    agent = make_agent(api, "Researcher")
    chat = chat_with(api, agent).json()
    with use_model(replies("Here's what I found.")):
        resp = api.post(
            f"/api/chats/{chat['id']}/messages",
            json={"body": "Find papers", "mentions": [{"kind": "agent", "id": agent["id"]}]},
        )
    assert resp.status_code == 201, resp.text
    assert replies_from(api, chat) == [("Researcher", "Here's what I found.")]


def test_someone_not_allowed_to_invoke_the_agent_is_refused_with_the_reason(api, participant):
    agent = make_agent(api, "Payroll")
    api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, invocable_by="editors"))
    before = {c["id"] for c in api.get("/api/chats").json()}

    with signed_in_as(participant):
        resp = chat_with(api, agent)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only editors and admins can invoke the agent Payroll."
    assert {c["id"] for c in api.get("/api/chats").json()} == before


def test_an_archived_or_unknown_agent_makes_no_chat(api):
    agent = make_agent(api, "Old")
    assert api.post(f"/api/agents/{agent['id']}/archive").is_success
    resp = chat_with(api, agent)
    assert resp.status_code == 409
    assert "archived" in resp.json()["detail"]
    assert api.post("/api/chats", json={"agent_id": agent["id"] + 1000}).status_code == 404


def test_a_plain_chat_still_gets_the_default_chat_agents(api):
    chat = make_chat(api, "Design")
    assert len(chat["members"]["agents"]) == 1
