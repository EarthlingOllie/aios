"""Mention guards, checked at posting in a fixed order before anything costs money: how many
agents and workflows one message mentions (422), each mention's invocable-by (a refused card),
the poster's mention rate (429), then the handoff. Participants mention like anyone else.

Through the API, as a signed-in member, with the scripted model."""

import pytest
from conftest import join_organisation
from test_agents import agent_body, make_agent
from test_chat_agent_invocations import replies_from
from test_chats import add, make_chat, messages, signed_in_as
from test_permissions import PARTICIPANT, set_role

from aios.config import get_settings
from aios.testing.fake_model import ToolCall, model, replies, use_handoff, use_model


def post(api, chat: dict, *mentioned: tuple[str, dict], body: str = "Hello"):
    return api.post(
        f"/api/chats/{chat['id']}/messages",
        json={"body": body, "mentions": [{"kind": k, "id": m["id"]} for k, m in mentioned]},
    )


def counted_handoffs(calls: list):
    """A handoff model that counts its calls."""

    async def script(messages, info):
        calls.append(1)
        yield ToolCall("Handoff", {"ack": "On it.", "brief": ""})

    return model(script)


@pytest.fixture
def participant(db, user):
    u = join_organisation(db, user, "participant", first_name="Pat", last_name="Participant")
    set_role(db, u, PARTICIPANT, of=user)
    return u


def test_a_message_mentioning_six_agents_answers_422_and_writes_nothing(api, db, user):
    chat = make_chat(api, "Ops")
    agents = [make_agent(api, f"Bot {i}") for i in range(6)]
    for agent in agents:
        add(api, chat, "agent", agent)
    before = messages(api, chat)

    resp = post(api, chat, *[("agent", a) for a in agents])

    assert resp.status_code == 422
    assert "at most 5 agents and workflows" in resp.json()["detail"]
    assert messages(api, chat) == before


def test_people_mentioned_dont_count_towards_the_cap(api, db, user, monkeypatch):
    monkeypatch.setattr(get_settings(), "mentions_per_message", 1)
    chat = make_chat(api, "Ops")
    agent = make_agent(api, "Bot")
    add(api, chat, "agent", agent)
    colleague = join_organisation(db, user, "colleague")
    with signed_in_as(colleague):
        assert api.post(f"/api/chats/{chat['id']}/join").is_success
    with use_model(replies("Hi.")):
        resp = post(
            api, chat, ("agent", agent), ("user", {"id": colleague.id}), ("user", {"id": user.id})
        )
    assert resp.status_code == 201, resp.text


def test_a_mention_refused_by_invocable_by_posts_with_a_refused_card_and_calls_no_model(
    api, participant
):
    guarded = make_agent(api, "Payroll")
    resp = api.put(f"/api/agents/{guarded['id']}", json=agent_body(guarded, invocable_by="editors"))
    assert (resp.status_code, resp.json()["invocable_by"]) == (200, "editors")
    open_to_all = make_agent(api, "Helper")
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", guarded)
    add(api, chat, "agent", open_to_all)
    handoffs: list = []

    with signed_in_as(participant):
        assert api.post(f"/api/chats/{chat['id']}/join").is_success
        with use_handoff(counted_handoffs(handoffs)), use_model(replies("Done.")):
            resp = post(api, chat, ("agent", guarded), ("agent", open_to_all), body="Pay and help")

    assert resp.status_code == 201, resp.text
    posted = resp.json()
    message, refused_card, other_card = posted["messages"]
    assert message["body"] == "Pay and help"
    refused, other = posted["invocations"]
    assert (refused["target_id"], refused["status"]) == (guarded["id"], "refused")
    reason = "Only editors and admins can invoke the agent Payroll."
    assert refused["refusal_reason"] == reason
    assert refused_card["body"] == reason and refused_card["kind"] == "run_card"
    assert refused["agent_run_id"] is None
    # The other mention ran; the refused one called no model, not even the handoff.
    assert other["target_id"] == open_to_all["id"]
    assert len(handoffs) == 1
    assert replies_from(api, chat) == [("Helper", "Done.")]


def test_admins_only_targets_refuse_editors_too(api, db, user):
    agent = make_agent(api, "Vault")
    api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, invocable_by="admins"))
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", agent)
    editor = join_organisation(db, user, "editor")
    with signed_in_as(editor):
        assert api.post(f"/api/chats/{chat['id']}/join").is_success
        resp = post(api, chat, ("agent", agent))
    [invocation] = resp.json()["invocations"]
    assert invocation["status"] == "refused"
    assert invocation["refusal_reason"] == "Only admins can invoke the agent Vault."
    # The admin may.
    with use_model(replies("Opened.")):
        resp = post(api, chat, ("agent", agent))
    assert resp.json()["invocations"][0]["status"] == "queued"


def test_over_the_mention_rate_answers_429_with_a_retry_time_and_writes_nothing(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "mention_rate_per_person", "2/minute")
    chat = make_chat(api, "Ops")
    a, b = make_agent(api, "A"), make_agent(api, "B")
    add(api, chat, "agent", a)
    add(api, chat, "agent", b)
    with use_model(replies("Hi.")):
        assert post(api, chat, ("agent", a), ("agent", b)).status_code == 201
        before = messages(api, chat)
        resp = post(api, chat, ("agent", a))

    assert resp.status_code == 429
    retry = int(resp.headers["Retry-After"])
    assert 1 <= retry <= 60
    assert f"Try again in {retry} seconds" in resp.json()["detail"]
    assert messages(api, chat) == before
    # A message mentioning no agent isn't limited.
    assert post(api, chat).status_code == 201


def test_refused_mentions_dont_count_towards_the_rate_limit(api, participant, monkeypatch):
    monkeypatch.setattr(get_settings(), "mention_rate_per_person", "1/minute")
    guarded = make_agent(api, "Payroll")
    api.put(f"/api/agents/{guarded['id']}", json=agent_body(guarded, invocable_by="admins"))
    helper = make_agent(api, "Helper")
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", guarded)
    add(api, chat, "agent", helper)
    with signed_in_as(participant):
        assert api.post(f"/api/chats/{chat['id']}/join").is_success
        for _ in range(3):
            assert post(api, chat, ("agent", guarded)).status_code == 201
        with use_model(replies("Hi.")):
            assert post(api, chat, ("agent", helper)).status_code == 201
            assert post(api, chat, ("agent", helper)).status_code == 429


def test_a_participant_mentions_an_agent_and_it_runs(api, participant):
    agent = make_agent(api, "Helper")
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", agent)
    with signed_in_as(participant):
        assert api.post(f"/api/chats/{chat['id']}/join").is_success
        with use_model(replies("At your service.")):
            resp = post(api, chat, ("agent", agent))
        assert resp.status_code == 201, resp.text
    assert replies_from(api, chat) == [("Helper", "At your service.")]
