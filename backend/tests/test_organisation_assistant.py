"""The organisation assistant: a built-in agent made with the organisation (by its creator, when
its store is provisioned), which searches everything the organisation knows and queries its data
tables; and the default chat agents, the ordered list of agents that join every new chat."""

import pytest
from conftest import join_organisation, store_of, user_store
from sqlalchemy.exc import IntegrityError
from test_agents import agent_body
from test_audit_log import entries
from test_chat_agent_invocations import replies_from, say
from test_chats import (  # noqa: F401
    add,
    join_as,
    make_chat,
    messages,
    other,
    outsider,
    signed_in_as,
)
from test_data_tools import as_owner, provisioned
from test_guards import participant, post  # noqa: F401
from test_organisation_scoping import member  # noqa: F401
from test_triggers import make_agent

from aios.datastores.models import Agent
from aios.models import Role
from aios.testing.fake_model import (
    ToolCall,
    briefs,
    last_tool_results,
    model,
    replies,
    tool_output,
    use_handoff,
    use_model,
)
from aios.tools import search

NAME = "Organisation assistant"


def assistant(api) -> dict:
    [found] = [a for a in api.get("/api/agents").json() if a["name"] == NAME]
    return found


def test_a_new_organisation_has_its_assistant_from_the_start(api, user):
    [agent] = api.get("/api/agents").json()
    assert (agent["name"], agent["created_by"]["id"]) == (NAME, user.id)
    full = api.get(f"/api/agents/{agent['id']}").json()["current_version"]
    assert [t["key"] for t in full["tools"]] == ["search", "describe_data", "query_data"]
    for words in ("search", "cite", "query", "untrusted", "nothing"):
        assert words in full["instructions"].lower()


def test_mentioned_in_a_chat_it_answers_from_search_and_query_data(
    api,
    db,
    user,
    other,  # noqa: F811
    monkeypatch,
):
    with as_owner(provisioned(db, store_of(user))) as conn:
        conn.execute("CREATE TABLE data.fish (id int PRIMARY KEY, weight_kg numeric)")
        conn.execute("INSERT INTO data.fish VALUES (1, 2.5), (2, 4)")
    searched = []

    async def found(*, data_store_id, query, limit, filters):
        searched.append((data_store_id, query))
        return [{"kind": "document_chunk", "source": "fish.md", "text": "We weigh our fish."}]

    monkeypatch.setattr(search, "search_results", found)
    chat = make_chat(api, "Design")
    join_as(api, chat, other)
    agent = assistant(api)

    async def script(msgs, info):
        results = last_tool_results(msgs)
        if not results:
            yield ToolCall("search", {"query": "fish weight"})
        elif isinstance(output := tool_output(results[0]), list):
            assert output[0]["source"] == "fish.md"
            yield ToolCall("query_data", {"sql": "SELECT sum(weight_kg) AS total FROM data.fish"})
        else:
            [row] = output["rows"]
            yield f"Our fish weigh {row[0]} kg in all [fish.md]."

    with (
        signed_in_as(other),
        use_handoff(briefs("I'll look that up.", "They weigh fish.")),
        use_model(model(script)),
    ):
        say(api, chat, f"@[{NAME}](agent:{agent['id']}) how heavy?", ("agent", agent))

    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["requested_by"]) == ("succeeded", other.id)
    assert invocation["agent_run"]["tool_call_count"] == 2
    assert searched == [(store_of(user), "fish weight")]
    assert replies_from(api, chat) == [(NAME, "Our fish weigh 6.5 kg in all [fish.md].")]


def test_an_organisation_has_at_most_one_live_assistant(api, user):
    agent = assistant(api)
    with user_store(user) as store, pytest.raises(IntegrityError):
        store.add(Agent(name="Another", builtin="assistant"))
        store.commit()
    # An archived one doesn't count.
    assert api.post(f"/api/agents/{agent['id']}/archive").status_code == 200
    with user_store(user) as store:
        store.add(Agent(name="Another", builtin="assistant"))
        store.commit()


DEFAULTS = "/api/organisation/default-chat-agents"


def defaults(api) -> list[tuple[str, bool]]:
    resp = api.get(DEFAULTS)
    assert resp.status_code == 200, resp.text
    return [(a["name"], a["archived"]) for a in resp.json()]


def agent_names(chat: dict) -> list[str]:
    return [a["name"] for a in chat["members"]["agents"]]


def test_the_default_chat_agents_start_as_the_assistant_and_join_each_new_chat(api):
    assert defaults(api) == [(NAME, False)]
    assert agent_names(make_chat(api, "Design")) == [NAME]


def test_admins_choose_the_default_chat_agents_in_order_and_it_is_audited(api, user):
    helper = make_agent(api, "Helper")
    agent = assistant(api)
    resp = api.put(DEFAULTS, json={"agent_ids": [helper["id"], agent["id"]]})
    assert resp.status_code == 200, resp.text
    assert [(a["id"], a["name"]) for a in resp.json()] == [
        (helper["id"], "Helper"),
        (agent["id"], NAME),
    ]
    assert agent_names(make_chat(api, "Design")) == ["Helper", NAME]
    [entry] = [e for e in entries(api) if e["action"] == "settings.changed"]
    assert entry["details"] == {
        "setting": "default_chat_agents",
        "from": [agent["id"]],
        "to": [helper["id"], agent["id"]],
    }
    # None at all is a choice too.
    assert api.put(DEFAULTS, json={"agent_ids": []}).json() == []
    assert agent_names(make_chat(api, "Planning")) == []


@pytest.mark.parametrize("role", [Role.editor, Role.participant])
def test_only_admins_change_the_default_chat_agents(api, db, user, role):
    from test_permissions import set_role

    someone = join_organisation(db, user, "someone")
    set_role(db, someone, role, of=user)
    with signed_in_as(someone):
        assert defaults(api) == [(NAME, False)]
        assert api.put(DEFAULTS, json={"agent_ids": []}).status_code == 403
    assert defaults(api) == [(NAME, False)]


def test_unknown_or_repeated_agents_are_refused(api):
    agent = assistant(api)
    assert api.put(DEFAULTS, json={"agent_ids": [agent["id"], 999999]}).status_code == 422
    assert api.put(DEFAULTS, json={"agent_ids": [agent["id"], agent["id"]]}).status_code == 422
    assert defaults(api) == [(NAME, False)]


def test_archived_default_agents_are_skipped_and_shown_archived(api):
    helper = make_agent(api, "Helper")
    agent = assistant(api)
    api.put(DEFAULTS, json={"agent_ids": [agent["id"], helper["id"]]})
    assert api.post(f"/api/agents/{agent['id']}/archive").status_code == 200
    assert defaults(api) == [(NAME, True), ("Helper", False)]
    assert agent_names(make_chat(api, "Design")) == ["Helper"]
    assert api.post(f"/api/agents/{agent['id']}/unarchive").status_code == 200
    assert agent_names(make_chat(api, "Planning")) == [NAME, "Helper"]


def test_changing_the_default_agents_changes_only_new_chats(api):
    helper = make_agent(api, "Helper")
    before = make_chat(api, "Before")
    api.put(DEFAULTS, json={"agent_ids": [helper["id"]]})
    assert agent_names(api.get(f"/api/chats/{before['id']}").json()) == [NAME]
    assert agent_names(make_chat(api, "After")) == ["Helper"]


def test_chat_with_this_agent_adds_no_default_agents(api):
    helper = make_agent(api, "Helper")
    resp = api.post("/api/chats", json={"agent_id": helper["id"]})
    assert resp.status_code == 201, resp.text
    assert agent_names(resp.json()) == ["Helper"]


def test_a_default_agent_the_creator_cant_invoke_is_still_added_and_mentions_refused(
    api,
    participant,  # noqa: F811
):
    guarded = make_agent(api, "Payroll")
    api.put(f"/api/agents/{guarded['id']}", json=agent_body(guarded, invocable_by="editors"))
    api.put(DEFAULTS, json={"agent_ids": [guarded["id"]]})
    with signed_in_as(participant):
        chat = make_chat(api, "Ops")
        assert agent_names(chat) == ["Payroll"]
        with use_model(replies("Paid.")):
            resp = post(api, chat, ("agent", guarded), body="Pay")
    assert resp.status_code == 201, resp.text
    _, card = resp.json()["messages"]
    [invocation] = resp.json()["invocations"]
    reason = "Only editors and admins can invoke the agent Payroll."
    assert (invocation["status"], invocation["refusal_reason"]) == ("refused", reason)
    assert (card["kind"], card["body"]) == ("run_card", reason)


def test_make_seed_makes_the_dev_organisations_assistant_as_creating_one_does(db):
    from conftest import delete_user, org_of
    from sqlalchemy import select

    from aios import seed
    from aios.datastores import data_store_for, store_session
    from aios.datastores.models import Chat, ChatMember
    from aios.models import User

    seed.build(db)
    made = db.scalar(select(User).where(User.email == seed.EMAIL))
    try:
        with store_session(data_store_for(org_of(db, made).id), "ro") as store:
            [(agent_id, created_by)] = store.execute(
                select(Agent.id, Agent.created_by).where(Agent.builtin == "assistant")
            ).all()
            assert created_by == made.id
            general = store.scalar(select(Chat.id).where(Chat.name == "General"))
            assert agent_id in store.scalars(
                select(ChatMember.agent_id).where(ChatMember.chat_id == general)
            )
    finally:
        delete_user(db, made)
