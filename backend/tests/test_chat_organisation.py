"""Chats belong to the organisation: "public" is every member of it, the owner adds any of its
agents, workflows and members, names are unique within it, each new chat gets its documents
assistant, and to anyone in another organisation its chats don't exist."""

from conftest import delete_user, join_organisation
from sqlalchemy import delete, update
from test_chats import add, add_user, make_agent, make_chat, make_workflow, post, signed_in_as
from test_organisation_scoping import member, outsider, person  # noqa: F401
from test_organisations import revision

from aios.models import Chat, ChatMember, ChatMemberKind, ChatMemberRole, ChatVisibility

NAME_TAKEN = "The organisation already has a chat with that name."


def test_another_organisation_cant_reach_a_chat_public_or_private(api, user, outsider):  # noqa: F811
    public = make_chat(api, "Open", visibility="public")
    private = make_chat(api, "Secret", visibility="private")
    for chat in (public, private):
        post(api, chat, "Hello")
    [owner] = public["members"]["users"]
    with signed_in_as(outsider):
        assert api.get("/api/chats").json() == []
        for chat in (public, private):
            url = f"/api/chats/{chat['id']}"
            body = {"name": "Mine now", "description": "", "visibility": "public"}
            responses = {
                "read": api.get(url),
                "messages": api.get(f"{url}/messages"),
                "mentionable": api.get(f"{url}/mentionable"),
                "join": api.post(f"{url}/join"),
                "leave": api.post(f"{url}/leave"),
                "post": api.post(f"{url}/messages", json={"body": "Hi"}),
                "mark read": api.post(f"{url}/read", json={"message_id": 1}),
                "change": api.put(url, json=body),
                "add member": api.post(f"{url}/members", json={"kind": "user", "id": outsider.id}),
                "remove member": api.delete(f"{url}/members/{owner['id']}"),
                "delete": api.delete(url),
            }
            got = {what: r.status_code for what, r in responses.items()}
            assert got == {what: 404 for what in responses}, chat["name"]
    # Untouched.
    assert [c["name"] for c in api.get("/api/chats").json()] == ["Secret", "Open"]


def test_public_means_every_member_of_the_organisation(api, user, member):  # noqa: F811
    chat = make_chat(api, "Open", visibility="public")
    make_chat(api, "Secret", visibility="private")
    with signed_in_as(member):
        listed = api.get("/api/chats").json()
        assert [(c["name"], c["is_member"]) for c in listed] == [("Open", False)]
        resp = api.post(f"/api/chats/{chat['id']}/join")
        assert resp.status_code == 200, resp.text
        post(api, chat, "Hi")


def test_chat_names_are_unique_within_the_organisation(api, member, outsider):  # noqa: F811
    make_chat(api, "general")
    # Another member's "general" is the same name in the same organisation.
    with signed_in_as(member):
        resp = api.post("/api/chats", json={"name": "general"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == NAME_TAKEN
        mine = make_chat(api, "random")
        body = {"name": "general", "description": "", "visibility": "private"}
        resp = api.put(f"/api/chats/{mine['id']}", json=body)
        assert resp.status_code == 409
        assert resp.json()["detail"] == NAME_TAKEN
    # Another organisation has a "general" of its own.
    with signed_in_as(outsider):
        theirs = make_chat(api, "general")
        assert [c["id"] for c in api.get("/api/chats").json()] == [theirs["id"]]


def test_the_owner_adds_another_members_agent_and_workflow_and_a_fellow_member(
    api,
    user,
    member,  # noqa: F811
    outsider,  # noqa: F811
):
    chat = make_chat(api, "Design")
    with signed_in_as(member):
        agent = make_agent(api, "Their reviewer")
        workflow = make_workflow(api, "Their digest")
    add(api, chat, "agent", agent)
    add(api, chat, "workflow", workflow)
    chat = add_user(api, chat, member)
    assert [m["name"] for m in chat["members"]["agents"]] == [
        "Documents assistant",
        "Their reviewer",
    ]
    assert [m["name"] for m in chat["members"]["workflows"]] == ["Their digest"]
    assert [m["target_id"] for m in chat["members"]["users"]] == [user.id, member.id]
    mentionable = api.get(f"/api/chats/{chat['id']}/mentionable").json()
    assert [m["id"] for m in mentionable["users"]] == [user.id, member.id]
    # A fellow member is theirs to mention.
    resp = api.post(
        f"/api/chats/{chat['id']}/messages",
        json={"body": "Over to you", "mentions": [{"kind": "user", "id": member.id}]},
    )
    assert resp.status_code == 201, resp.text

    # Nothing of another organisation's, and nobody from it: they don't exist here.
    with signed_in_as(outsider):
        theirs = make_agent(api, "Outsider's agent")
        their_workflow = make_workflow(api, "Outsider's workflow")
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": outsider.id})
    assert resp.status_code == 404
    resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": -999})
    assert resp.status_code == 404
    for kind, target in (("agent", theirs), ("workflow", their_workflow)):
        resp = api.post(f"/api/chats/{chat['id']}/members", json={"kind": kind, "id": target["id"]})
        assert resp.status_code == 422, kind
        assert resp.json()["detail"] == f"Unknown {kind}"
    assert api.get(f"/api/chats/{chat['id']}").json()["member_count"] == 5


def test_a_new_chats_first_agent_is_the_organisations_documents_assistant(
    api,
    member,  # noqa: F811
    outsider,  # noqa: F811
):
    mine = make_chat(api, "Mine")
    with signed_in_as(member):
        theirs = make_chat(api, "Theirs")
    with signed_in_as(outsider):
        elsewhere = make_chat(api, "Elsewhere")
    [assistant] = mine["members"]["agents"]
    assert assistant["name"] == "Documents assistant"
    # The same agent for every member's chat, whoever made it.
    assert [a["target_id"] for a in theirs["members"]["agents"]] == [assistant["target_id"]]
    [their_assistant] = elsewhere["members"]["agents"]
    assert their_assistant["target_id"] != assistant["target_id"]


def test_a_chat_says_who_made_it_and_who_last_changed_it(api, user, member):  # noqa: F811
    chat = make_chat(api, "Design", visibility="public")
    assert (chat["created_by"], chat["updated_by"]) == (person(user), person(user))
    with signed_in_as(member):
        [listed] = api.get("/api/chats").json()
        assert (listed["created_by"], listed["updated_by"]) == (person(user), person(user))
        body = {"name": "Design", "description": "Pixels", "visibility": "public"}
        assert api.put(f"/api/chats/{chat['id']}", json=body).status_code == 403
    body = {"name": "Product design", "description": "", "visibility": "public"}
    changed = api.put(f"/api/chats/{chat['id']}", json=body).json()
    assert (changed["created_by"], changed["updated_by"]) == (person(user), person(user))
    assert api.get(f"/api/chats/{chat['id']}").json()["created_by"] == person(user)


def test_the_upgrade_takes_users_of_other_organisations_out_of_chats(
    api,
    db,
    user,
    member,  # noqa: F811
    outsider,  # noqa: F811
):
    """Revision 0043, on a chat joined while "public" meant every user of the deployment."""
    chat = make_chat(api, "Open", visibility="public")
    add_user(api, chat, member)
    db.add(
        ChatMember(
            chat_id=chat["id"],
            kind=ChatMemberKind.user,
            user_id=outsider.id,
            role=ChatMemberRole.member,
        )
    )
    db.commit()
    # Seated, but the chat doesn't exist for them: it's another organisation's.
    with signed_in_as(outsider):
        assert api.get(f"/api/chats/{chat['id']}").status_code == 404

    revision("0043_chat_names_unique_per_organisation").drop_outsiders(db.connection())
    db.commit()

    users = api.get(f"/api/chats/{chat['id']}").json()["members"]["users"]
    assert [m["target_id"] for m in users] == [user.id, member.id]


def owners(api, chat: dict) -> list[int]:
    """The users who hold the chat's owner role, as its members list says."""
    users = api.get(f"/api/chats/{chat['id']}").json()["members"]["users"]
    return [m["target_id"] for m in users if m["role"] == "owner"]


def test_a_chat_owner_who_leaves_the_organisation_hands_it_to_the_member_added_first(
    api,
    db,
    user,
    member,  # noqa: F811
):
    third = join_organisation(db, user, -60, "third")
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Handover", visibility="private")
            add_user(api, chat, user)
            add_user(api, chat, third)
            assert api.post("/api/organisation/leave").status_code == 200

        # The chat stays the organisation's, with the member who was added first as its owner.
        got = api.get(f"/api/chats/{chat['id']}").json()
        assert got["is_owner"] is True
        assert owners(api, chat) == [user.id]
        body = {"name": "Ours now", "description": "", "visibility": "private"}
        assert api.put(f"/api/chats/{chat['id']}", json=body).status_code == 200
        with signed_in_as(third):
            assert api.get(f"/api/chats/{chat['id']}").json()["is_owner"] is False
            assert api.put(f"/api/chats/{chat['id']}", json=body).status_code == 403
    finally:
        delete_user(db, third)


def test_a_chat_its_owner_left_with_nobody_in_it_goes_to_whoever_joins_it(
    api,
    user,
    member,  # noqa: F811
):
    with signed_in_as(member):
        chat = make_chat(api, "Abandoned", visibility="public")
        assert api.post("/api/organisation/leave").status_code == 200

    [listed] = api.get("/api/chats").json()
    assert (listed["is_member"], listed["is_owner"]) == (False, False)
    joined = api.post(f"/api/chats/{chat['id']}/join").json()
    assert joined["is_owner"] is True
    assert owners(api, chat) == [user.id]
    assert api.delete(f"/api/chats/{chat['id']}").status_code == 204


def test_a_private_chat_its_owner_left_with_nobody_in_it_goes_public_for_whoever_joins_it(
    api,
    user,
    member,  # noqa: F811
):
    """Private, with nobody in it, it would exist for nobody: nobody could join it, so nobody could
    own, change or delete it, and its name would stay taken. Public, the organisation keeps it."""
    with signed_in_as(member):
        chat = make_chat(api, "Secrets", visibility="private")
        post(api, chat, "Notes for the team")
        assert api.post("/api/organisation/leave").status_code == 200

    [listed] = api.get("/api/chats").json()
    assert (listed["name"], listed["visibility"], listed["is_member"]) == (
        "Secrets",
        "public",
        False,
    )
    [message] = api.get(f"/api/chats/{chat['id']}/messages").json()["messages"]
    assert message["body"] == "Notes for the team"
    assert api.post(f"/api/chats/{chat['id']}/join").json()["is_owner"] is True
    assert api.delete(f"/api/chats/{chat['id']}").status_code == 204
    assert api.post("/api/chats", json={"name": "Secrets"}).status_code == 201


def test_a_private_chat_with_another_user_in_it_stays_private_when_its_owner_leaves(
    api,
    user,
    member,  # noqa: F811
):
    with signed_in_as(member):
        chat = make_chat(api, "Two of us", visibility="private")
        add_user(api, chat, user)
        assert api.post("/api/organisation/leave").status_code == 200

    got = api.get(f"/api/chats/{chat['id']}").json()
    assert (got["visibility"], got["is_owner"]) == ("private", True)


def test_the_upgrade_hands_a_chat_whose_owner_left_to_the_member_added_first(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """Revision 0048, on a chat whose owner left the organisation before leaving handed it over:
    their member row went with their membership, and nobody held the owner role."""
    with signed_in_as(member):
        chat = make_chat(api, "Left behind", visibility="private")
        add_user(api, chat, user)
    db.execute(
        delete(ChatMember).where(ChatMember.chat_id == chat["id"], ChatMember.user_id == member.id)
    )
    db.commit()
    assert owners(api, chat) == []

    revision("0048_organisations_contract").hand_over(db.connection())
    db.commit()

    assert owners(api, chat) == [user.id]


def test_the_upgrade_makes_a_private_chat_with_nobody_to_own_it_public(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """Revision 0049, on a private chat its owner left with nobody in it before leaving made it
    public: 0048's hand-over found nobody to give it to."""
    with signed_in_as(member):
        abandoned = make_chat(api, "Abandoned", visibility="private")
        kept = make_chat(api, "Kept", visibility="private")
        add_user(api, kept, user)
        assert api.post("/api/organisation/leave").status_code == 200
    # As leaving left them before: the abandoned one private, with nobody to own it.
    db.execute(
        update(Chat).where(Chat.id == abandoned["id"]).values(visibility=ChatVisibility.private)
    )
    db.commit()
    assert api.get(f"/api/chats/{abandoned['id']}").status_code == 404

    revision("0049_ownerless_private_chats").publicise(db.connection())
    db.commit()

    got = {c["name"]: c["visibility"] for c in api.get("/api/chats").json()}
    assert got == {"Abandoned": "public", "Kept": "private"}
    assert api.post(f"/api/chats/{abandoned['id']}/join").json()["is_owner"] is True
