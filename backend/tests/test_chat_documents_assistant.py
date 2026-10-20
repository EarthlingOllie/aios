"""The documents assistant: a built-in member of every new chat, answering over the documents of
the chat's owner the way any agent member answers a mention."""

from conftest import store_of
from fake_model import (
    ToolCall,
    briefs,
    last_tool_results,
    model,
    tool_output,
    use_handoff,
    use_model,
)
from test_chat_agent_invocations import replies_from, say
from test_chats import (  # noqa: F401
    add,
    add_user,
    make_chat,
    messages,
    other,
    outsider,
    signed_in_as,
)
from test_organisation_scoping import member  # noqa: F401
from test_triggers import make_agent

from aios import checkpoints
from aios.tools import hybrid_search

NAME = "Documents assistant"


def assistant_of(chat: dict) -> dict:
    [found] = [m for m in chat["members"]["agents"] if m["name"].startswith(NAME)]
    return found


def test_a_new_chat_has_the_documents_assistant_as_a_member(api):
    chat = make_chat(api, "Design")

    [assistant] = chat["members"]["agents"]
    assert (assistant["kind"], assistant["name"]) == ("agent", NAME)
    assert chat["member_count"] == 2
    # Mentionable, under the agents.
    mentionable = api.get(f"/api/chats/{chat['id']}/mentionable").json()
    assert [(m["kind"], m["id"], m["name"]) for m in mentionable["agents"]] == [
        ("agent", assistant["target_id"], NAME)
    ]
    # It's the user's own agent, with document search: one per user, in every chat they make.
    agent = api.get(f"/api/agents/{assistant['target_id']}").json()
    assert [t["key"] for t in agent["current_version"]["tools"]] == ["hybrid_search"]
    assert "documents" in agent["current_version"]["instructions"]
    again = make_chat(api, "Planning")
    assert assistant_of(again)["target_id"] == assistant["target_id"]
    assert [a["name"] for a in api.get("/api/agents").json()] == [NAME]


def test_each_organisation_has_its_own_assistant(api, outsider):  # noqa: F811
    mine = assistant_of(make_chat(api, "Mine"))
    with signed_in_as(outsider):
        theirs = assistant_of(make_chat(api, "Theirs"))
        assert [a["id"] for a in api.get("/api/agents").json()] == [theirs["target_id"]]
    assert theirs["target_id"] != mine["target_id"]


def test_the_members_of_an_organisation_share_one_assistant(api, user, member):  # noqa: F811
    # Made for whichever member first needs it.
    with signed_in_as(member):
        theirs = assistant_of(make_chat(api, "Theirs"))
    mine = assistant_of(make_chat(api, "Mine"))
    assert mine["target_id"] == theirs["target_id"]
    agent = api.get(f"/api/agents/{mine['target_id']}").json()
    assert agent["created_by"]["id"] == member.id
    assert [a["id"] for a in api.get("/api/agents").json()] == [mine["target_id"]]


def test_an_agent_of_the_same_name_doesnt_stop_the_assistant_joining(api):
    mine = make_agent(api, NAME)
    chat = make_chat(api, "Design")
    assistant = assistant_of(chat)
    assert assistant["target_id"] != mine["id"]
    assert assistant["name"] == f"{NAME} 2"


def test_a_deleted_assistant_is_made_again_for_the_next_chat(api):
    first = assistant_of(make_chat(api, "Design"))
    assert api.delete(f"/api/agents/{first['target_id']}").status_code == 204

    second = assistant_of(make_chat(api, "Planning"))
    assert second["target_id"] != first["target_id"]
    assert second["name"] == NAME


def test_mentioning_the_assistant_searches_the_chat_owners_documents_and_posts_the_reply(
    api,
    user,
    other,  # noqa: F811
    monkeypatch,
):
    searched = []

    async def search(*, data_store_id, query, limit):
        searched.append((data_store_id, query))
        return [{"source": "fish.md", "headings": [], "text": "Zebrafish."}]

    monkeypatch.setattr(hybrid_search, "document_search_results", search)
    chat = make_chat(api, "Design")
    add_user(api, chat, other)
    assistant = assistant_of(chat)
    target = {"id": assistant["target_id"]}

    async def script(msgs, info):
        if found := last_tool_results(msgs):
            yield f"Found {tool_output(found[0])[0]['text']} [fish.md]"
        else:
            yield ToolCall("search_documents", {"query": "fish"})

    # Another member asks: the assistant still answers from the owner's documents.
    with (
        signed_in_as(other),
        use_handoff(briefs("I'll look for fish in the documents.", "They study fish.")),
        use_model(model(script)),
    ):
        posted = say(api, chat, f"@[{NAME}](agent:{target['id']}) fish?", ("agent", target))

    message, card = posted["messages"]
    assert (card["kind"], card["author"]["name"]) == ("run_card", NAME)
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["requested_by"]) == ("succeeded", other.id)
    assert invocation["acknowledgement"] == "I'll look for fish in the documents."
    assert invocation["brief"] == "They study fish."
    assert invocation["agent_run"]["tool_call_count"] == 1
    assert searched == [(store_of(user), "fish")]
    history = checkpoints.history(invocation["agent_run_id"])
    assert history[0].text == f"They study fish.\n\n@{NAME} fish?"
    assert replies_from(api, chat) == [(NAME, "Found Zebrafish. [fish.md]")]
