"""Live updates: a chat's stream and a person's, read through the app as a browser reads them
(`sse.open_stream`)."""

import asyncio
from pathlib import Path

import pytest
import redis
from conftest import delete_user, join_organisation, store_of
from sse import Event, messages, open_stream
from test_chat_agent_invocations import say
from test_chats import (  # noqa: F401 (fixtures)
    add,
    make_agent,
    make_chat,
    other,
    outsider,
    post,
    signed_in_as,
)

from aios import streams
from aios.api import streams as streams_api
from aios.config import get_settings
from aios.testing.fake_model import model, replies, use_model


def stream_of(api, chat: dict, **kwargs):
    return open_stream(api, f"/api/chats/{chat['id']}/stream", **kwargs)


def ids(events: list[Event]) -> list[int]:
    return [e.id for e in messages(events)]


def statuses(events: list[Event], invocation_id: int) -> list[str]:
    """The invocation's states as its events give them, each once in a row."""
    seen: list[str] = []
    for e in events:
        mine = e.event == "invocation" and e.data["id"] == invocation_id
        if mine and (not seen or seen[-1] != e.data["status"]):
            seen.append(e.data["status"])
    return seen


def drafts(events: list[Event]) -> list[str]:
    return [e.data["text"] for e in events if e.event == "draft"]


@pytest.fixture
def quick_drafts(monkeypatch):
    """Every bit of a reply published as it's written."""
    monkeypatch.setattr(get_settings(), "draft_publish_interval_seconds", 0)


# --- The chat stream ------------------------------------------------------------------------------


def test_new_messages_arrive_on_the_chat_stream(api, user, other):  # noqa: F811
    chat = make_chat(api, "Design")
    with stream_of(api, chat) as stream:
        assert stream.status == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        mine = post(api, chat, "Hello")
        with signed_in_as(other):
            theirs = post(api, chat, "Hi **there**")
        events = stream.until(lambda es: len(messages(es)) == 2)
    first, second = messages(events)
    assert (first.id, first.data["body"], first.data["author"]["id"]) == (
        mine["id"],
        "Hello",
        user.id,
    )
    # As the messages read returns it, the message id the event id.
    assert second.data == theirs and second.id == theirs["id"]


def test_a_reconnect_catches_up_exactly_once(api, monkeypatch):
    chat = make_chat(api, "Design")
    seen = post(api, chat, "Seen before the drop")
    missed = [post(api, chat, f"Missed {i}") for i in range(3)]
    read = streams_api.chat_catch_up
    between: list[dict] = []

    def catch_up(*args):
        # Posted once the stream has subscribed, before it reads what it missed: it's in both
        # the read and what was published, and arrives once.
        between.append(post(api, chat, "Between subscribing and reading"))
        return read(*args)

    monkeypatch.setattr(streams_api, "chat_catch_up", catch_up)
    with stream_of(api, chat, last_event_id=seen["id"]) as stream:
        stream.until(lambda es: between and between[0]["id"] in ids(es))
        after = post(api, chat, "After")
        events = stream.until(lambda es: after["id"] in ids(es))
    assert ids(events) == [m["id"] for m in [*missed, *between, after]]


def test_a_reconnect_without_a_gap_sends_nothing_again(api):
    chat = make_chat(api, "Design")
    last = post(api, chat, "Already shown")
    with stream_of(api, chat, last_event_id=last["id"]) as stream:
        new = post(api, chat, "New")
        events = stream.until(lambda es: new["id"] in ids(es))
    assert ids(events) == [new["id"]]


def test_a_run_card_moves_from_queued_to_finished_on_the_stream(api):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    with stream_of(api, chat) as stream, use_model(replies("Done.")):
        posted = say(api, chat, f"@[Bot](agent:{agent['id']}) go", ("agent", agent))
        [invocation] = posted["invocations"]
        events = stream.until(lambda es: statuses(es, invocation["id"])[-1:] == ["succeeded"])
    assert statuses(events, invocation["id"]) == ["queued", "briefing", "running", "succeeded"]
    # The card's run, as the run page shows it, comes with it.
    finished = [e for e in events if e.event == "invocation"][-1].data
    assert finished["agent_run"]["status"] == "succeeded"
    reply = messages(events)[-1].data
    assert (reply["body"], reply["invocation_id"]) == ("Done.", invocation["id"])


def test_a_draft_grows_and_the_reply_replaces_it(api, user, quick_drafts, monkeypatch):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    mid_reply: dict = {}

    async def script(msgs, info):
        yield "Hel"
        await asyncio.sleep(0.2)
        yield "lo, "
        await asyncio.sleep(0.2)
        # Opening the chat now shows the draft so far; the database has none of it.
        late = await asyncio.to_thread(stream_of, api, chat)
        mid_reply["late"] = await asyncio.to_thread(
            late.until, lambda es: any(e.event == "draft" for e in es)
        )
        await asyncio.to_thread(late.close)
        mid_reply["read"] = await asyncio.to_thread(
            lambda: api.get(f"/api/chats/{chat['id']}/messages").json()
        )
        yield "world"

    with stream_of(api, chat) as stream, use_model(model(script)):
        posted = say(api, chat, f"@[Bot](agent:{agent['id']}) hi", ("agent", agent))
        [invocation] = posted["invocations"]
        events = stream.until(lambda es: statuses(es, invocation["id"])[-1:] == ["succeeded"])

    # The whole reply so far, each time.
    assert drafts(events) == ["Hel", "Hello, ", "Hello, world"]
    assert {e.data["invocation_id"] for e in events if e.event == "draft"} == {invocation["id"]}
    # The reply comes after the drafts, as the invocation's message, then the invocation ends.
    order = [e.event for e in events if e.event in ("draft", "message")]
    assert order[-4:] == ["draft", "draft", "draft", "message"]
    reply = messages(events)[-1].data
    assert (reply["body"], reply["invocation_id"]) == ("Hello, world", invocation["id"])

    late = mid_reply["late"]
    assert drafts(late) == ["Hello, "]
    assert [e.data["status"] for e in late if e.event == "invocation"] == ["running"]
    # Only the message and its card were stored mid-reply: never the draft.
    assert [m["body"] for m in mid_reply["read"]["messages"]][0].endswith("hi")
    assert len(mid_reply["read"]["messages"]) == 2
    # The turn over, nothing is left of it in Redis, and a newcomer sees no draft.
    monkeypatch.setattr(get_settings(), "stream_heartbeat_seconds", 0.1)
    with stream_of(api, chat) as after:
        # Its first heartbeat comes after what it catches up with.
        assert drafts(after.until(lambda es: any(e.event == "ping" for e in es))) == []
    with redis.Redis.from_url(get_settings().redis_url) as client:
        key = streams.draft_key(store_of(user), chat["id"], invocation["id"])
        assert client.get(key) is None


def test_a_failed_turn_drops_its_draft_and_shows_the_failure(api, user, quick_drafts):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)

    async def script(msgs, info):
        yield "Half an"
        await asyncio.sleep(0.2)
        raise RuntimeError("The model fell over")

    with stream_of(api, chat) as stream, use_model(model(script)):
        posted = say(api, chat, f"@[Bot](agent:{agent['id']}) hi", ("agent", agent))
        [invocation] = posted["invocations"]
        events = stream.until(lambda es: statuses(es, invocation["id"])[-1:] == ["failed"])

    assert drafts(events) == ["Half an"]
    failed = [e for e in events if e.event == "invocation"][-1].data
    assert failed["error"] == "The model fell over"
    assert messages(events)[-1].data["body"] == "The run failed: The model fell over"
    stored = api.get(f"/api/chats/{chat['id']}/messages").json()["messages"]
    assert all("Half an" not in m["body"] for m in stored)
    with redis.Redis.from_url(get_settings().redis_url) as client:
        assert client.get(streams.draft_key(store_of(user), chat["id"], invocation["id"])) is None


def test_anyone_in_the_organisation_opens_a_chats_stream_and_nobody_else(
    api,
    user,
    other,  # noqa: F811
    outsider,  # noqa: F811
):
    chat = make_chat(api, "Design")
    # `other` hasn't joined it.
    with signed_in_as(other), stream_of(api, chat) as stream:
        assert stream.status == 200
    with signed_in_as(outsider):
        org_id = api._organisation_id()
    with signed_in_as(outsider):
        nosy = open_stream(api, f"/api/o/{org_id}/chats/{chat['id']}/stream")
        assert nosy.status == 404
        mine = open_stream(api, f"/api/chats/{chat['id']}/stream")
        # In their own organisation, there's no such chat.
        assert mine.status == 404


def test_leaving_the_organisation_closes_its_streams_with_why(api, user, db):
    leaver = join_organisation(db, user, "leaver")
    try:
        chat = make_chat(api, "Design")
        with signed_in_as(leaver):
            stream = stream_of(api, chat)
            person = open_stream(api, "/api/me/stream")
            assert api.post("/api/organisation/leave").status_code == 200
        events = stream.until(lambda es: any(e.event == "gone" for e in es))
        stream.wait_ended()
        assert [e.data for e in events if e.event == "gone"] == [{"reason": "left_organisation"}]
        # Their chat list hears of it, across the organisations they're still in.
        listed = person.until(lambda es: any(e.event == "chat_list" for e in es))
        assert [e.data["change"] for e in listed if e.event == "chat_list"] == ["left_organisation"]
        person.close()
    finally:
        delete_user(db, leaver)


def test_leaving_a_chat_doesnt_close_its_stream(api, user):
    chat = make_chat(api, "Design")
    with stream_of(api, chat) as stream:
        assert api.post(f"/api/chats/{chat['id']}/leave").status_code == 204
        events = stream.until(lambda es: any(e.event == "members" for e in es))
        later = post(api, chat, "Back again")
        events = stream.until(lambda es: later["id"] in ids(es))
    assert not any(e.event == "gone" for e in events)


def test_archiving_turns_an_open_chat_read_only_without_closing_it(api, user):
    chat = make_chat(api, "Design")
    with stream_of(api, chat) as stream:
        assert api.post(f"/api/chats/{chat['id']}/archive").status_code == 200
        events = stream.until(lambda es: any(e.event == "chat" for e in es))
        [header] = [e.data for e in events if e.event == "chat"]
        assert header["id"] == chat["id"] and header["archived_at"] is not None
        assert api.post(f"/api/chats/{chat['id']}/unarchive").status_code == 200
        events = stream.until(lambda es: len([e for e in es if e.event == "chat"]) == 2)
    assert [e.data for e in events if e.event == "chat"][-1]["archived_at"] is None
    assert not stream.ended or not any(e.event == "gone" for e in events)


# --- The person stream ----------------------------------------------------------------------------


def test_unread_counts_and_chat_list_changes_arrive_on_the_person_stream(
    api,
    user,
    other,  # noqa: F811
):
    mine = make_chat(api, "Design")
    with open_stream(api, "/api/me/stream") as stream:
        with signed_in_as(other):
            post(api, mine, "One")
            second = post(api, mine, "Two")
        activity = stream.until(
            lambda es: any(
                e.event == "chat_activity" and e.data["message_id"] == second["id"] for e in es
            )
        )
        with signed_in_as(other):
            theirs = make_chat(api, "Theirs")
        assert api.post(f"/api/chats/{theirs['id']}/join").status_code == 200
        assert (
            api.put(f"/api/chats/{theirs['id']}", json={"name": "Ours", "description": ""})
        ).status_code == 200
        assert api.post(f"/api/chats/{theirs['id']}/archive").status_code == 200
        assert api.post(f"/api/chats/{theirs['id']}/unarchive").status_code == 200
        assert api.post(f"/api/chats/{theirs['id']}/leave").status_code == 204

        def changes(es):
            return [
                e.data["change"]
                for e in es
                if e.event == "chat_list" and e.data["chat_id"] == theirs["id"]
            ]

        events = stream.until(lambda es: changes(es)[-1:] == ["left"])
    [last] = [
        e.data
        for e in activity
        if e.event == "chat_activity" and e.data["message_id"] == second["id"]
    ]
    assert (last["chat_id"], last["unread_count"]) == (mine["id"], 2)
    assert changes(events) == ["appeared", "joined", "renamed", "archived", "unarchived", "left"]
    renamed = [e.data for e in events if e.event == "chat_list"][2]
    assert renamed["name"] == "Ours"


# --- Limits, heartbeats and Redis -----------------------------------------------------------------


def test_over_the_per_process_cap_a_stream_is_a_503_with_a_retry_hint(api, monkeypatch):
    chat = make_chat(api, "Design")
    monkeypatch.setattr(get_settings(), "streams_per_process", 1)
    with stream_of(api, chat) as first:
        assert first.status == 200
        refused = stream_of(api, chat)
        assert refused.status == 503
        assert refused.headers["retry-after"] == str(get_settings().stream_retry_seconds)
    # Once one closes, there's room again.
    with stream_of(api, chat) as again:
        assert again.status == 200


def test_over_the_per_person_cap_the_oldest_stream_is_replaced(api, monkeypatch):
    chat = make_chat(api, "Design")
    monkeypatch.setattr(get_settings(), "streams_per_person", 2)
    oldest, middle = stream_of(api, chat), open_stream(api, "/api/me/stream")
    try:
        with stream_of(api, chat) as newest:
            events = oldest.until(lambda es: any(e.event == "gone" for e in es))
            oldest.wait_ended()
            assert [e.data for e in events if e.event == "gone"] == [{"reason": "replaced"}]
            assert not middle.ended and newest.status == 200
            fresh = post(api, chat, "Still live")
            newest.until(lambda es: fresh["id"] in ids(es))
    finally:
        oldest.close()
        middle.close()


def test_heartbeats_run_on_the_configured_interval(api, monkeypatch):
    chat = make_chat(api, "Design")
    monkeypatch.setattr(get_settings(), "stream_heartbeat_seconds", 0.2)
    with stream_of(api, chat) as stream, open_stream(api, "/api/me/stream") as person:
        stream.until(lambda es: len([e for e in es if e.event == "ping"]) >= 3, timeout=3)
        person.until(lambda es: len([e for e in es if e.event == "ping"]) >= 3, timeout=3)


def test_with_redis_down_streams_answer_503_and_recover_once_its_back(api, monkeypatch):
    chat = make_chat(api, "Design")
    url = get_settings().redis_url
    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")
    for path in (f"/api/chats/{chat['id']}/stream", "/api/me/stream"):
        down = open_stream(api, path)
        assert down.status == 503
        assert down.headers["retry-after"] == str(get_settings().stream_retry_seconds)
    # Posting still works: what's committed is in the database, read when streams are back.
    missed = post(api, chat, "While Redis was down")
    monkeypatch.setattr(get_settings(), "redis_url", url)
    with stream_of(api, chat, last_event_id=missed["id"] - 1) as back:
        assert back.status == 200
        back.until(lambda es: missed["id"] in ids(es))


# --- Proxies --------------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("conf", ["frontend/nginx.compose.conf", "frontend/nginx.fullstack.conf"])
def test_the_proxies_dont_buffer_streams_and_outlast_the_heartbeat(conf):
    text = (ROOT / conf).read_text()
    block = text[text.index("location ~ ^/api(/|$)") :]
    block = block[: block.index("}")]
    assert "proxy_buffering off;" in block
    timeout = block.split("proxy_read_timeout")[1].split(";")[0].strip()
    assert int(timeout.removesuffix("s")) > get_settings().stream_heartbeat_seconds


def test_the_load_balancer_outlasts_the_heartbeat():
    # In AWS the load balancer routes the API (the web image's nginx serves only the app).
    alb = (ROOT / "infra/terraform/alb.tf").read_text()
    timeout = int(alb.split("idle_timeout")[1].split("=")[1].split()[0])
    assert timeout > get_settings().stream_heartbeat_seconds
