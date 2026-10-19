"""Chats belong to the organisation: "public" is every member of it, the owner adds any of its
agents, workflows and members, names are unique within it, each new chat gets its documents
assistant, and to anyone in another organisation its chats don't exist."""

import threading
from contextlib import contextmanager

from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import delete_user, join_organisation
from sqlalchemy import delete, select, update
from test_chats import add, add_user, make_agent, make_chat, make_workflow, post, signed_in_as
from test_organisation_scoping import member, outsider, person  # noqa: F401
from test_organisations import in_the_background, revision

from aios import organisations
from aios.db import SessionLocal
from aios.models import Chat, ChatMember, ChatMemberKind, ChatMemberRole, ChatVisibility, User

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


@contextmanager
def leaving(user: User):
    """The user's leave of their organisation, as `POST /api/organisation/leave` makes it, held
    uncommitted (flushed: their membership and chat rows gone, the organisation locked) for the
    block, then committed: a leave under way while the block's requests run."""
    with SessionLocal() as db:
        org = organisations.membership_of(db, user.id)
        leaver = db.get_one(User, user.id)
        assert organisations.leave_refusal(db, org, leaver) is None
        organisations.leave(db, org, leaver)
        db.flush()
        yield
        db.commit()


def users_in(db, chat: dict) -> list[int]:
    """The chat's user members, in the order they were added, whoever can see it."""
    db.expire_all()
    return list(
        db.scalars(
            select(ChatMember.user_id)
            .where(ChatMember.chat_id == chat["id"], ChatMember.kind == ChatMemberKind.user)
            .order_by(ChatMember.id)
        )
    )


def test_a_member_added_to_a_chat_as_they_leave_the_organisation_isnt_added(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """The owner adds Carol while her leave is under way: the add saw her a member, and her leave
    has already taken her out of its chats. Had it committed her row, she'd be in a chat of an
    organisation she isn't in, and the member added first, so its owner when its owner leaves:
    an owner it doesn't exist for, and nobody left in the organisation who could manage it."""
    carol = join_organisation(db, user, -61, "carol")
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Plans", visibility="private")
            with leaving(carol):
                adding, result = in_the_background(
                    lambda: api.post(
                        f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": carol.id}
                    )
                )
                adding.join(timeout=0.5)
            adding.join(timeout=10)

            assert "error" not in result, result
            assert result["value"].status_code == 404, result["value"].text
            assert result["value"].json()["detail"] == "Unknown user"
            assert users_in(db, chat) == [member.id]
            # So when the owner leaves, the chat goes to whoever's still in the organisation.
            add_user(api, chat, user)
            assert api.post("/api/organisation/leave").status_code == 200
        assert owners(api, chat) == [user.id]
    finally:
        delete_user(db, carol)


def test_a_chat_made_as_its_maker_leaves_the_organisation_isnt_made(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """Carol makes a chat (in another tab, say) while her leave is under way: the request was for
    the organisation she's leaving. Made, it would be owned by someone it doesn't exist for."""
    carol = join_organisation(db, user, -61, "carol")
    try:
        with signed_in_as(carol):
            with leaving(carol):
                making, result = in_the_background(
                    lambda: api.post("/api/chats", json={"name": "Mine", "visibility": "private"})
                )
                making.join(timeout=0.5)
            making.join(timeout=10)

        assert "error" not in result, result
        assert result["value"].status_code == 404, result["value"].text
        assert api.get("/api/chats").json() == []
        assert api.post("/api/chats", json={"name": "Mine"}).status_code == 201
    finally:
        delete_user(db, carol)


def test_a_chat_joined_as_its_joiner_leaves_the_organisation_isnt_joined(
    api,
    db,
    user,
    member,  # noqa: F811
):
    """Carol joins a public chat while her leave is under way. Joined, she'd be in a chat of an
    organisation she isn't in, and the member added first when its owner leaves."""
    carol = join_organisation(db, user, -61, "carol")
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Open", visibility="public")
        with signed_in_as(carol):
            with leaving(carol):
                joining, result = in_the_background(
                    lambda: api.post(f"/api/chats/{chat['id']}/join")
                )
                joining.join(timeout=0.5)
            joining.join(timeout=10)

        assert "error" not in result, result
        assert result["value"].status_code == 404, result["value"].text
        assert users_in(db, chat) == [member.id]
    finally:
        delete_user(db, carol)


def test_a_member_who_leaves_as_theyre_added_to_a_chat_is_taken_out_of_it(
    api,
    db,
    user,
    member,  # noqa: F811
    monkeypatch,
):
    """The other order: the add has seen Carol a member, and holds that, when her leave starts.
    The leave waits for the add (it doesn't deadlock with it), then takes her out of the chat."""
    carol = join_organisation(db, user, -61, "carol")
    checked, go_on = threading.Event(), threading.Event()
    holds_member = organisations.holds_member

    def pausing(db, org, user_id):
        held = holds_member(db, org, user_id)
        if user_id == carol.id:
            checked.set()
            assert go_on.wait(10)
        return held

    monkeypatch.setattr(organisations, "holds_member", pausing)
    try:
        with signed_in_as(member):
            chat = make_chat(api, "Plans", visibility="private")
            adding, added = in_the_background(
                lambda: api.post(
                    f"/api/chats/{chat['id']}/members", json={"kind": "user", "id": carol.id}
                )
            )
            assert checked.wait(10), added
        with signed_in_as(carol):
            leaving_it, left = in_the_background(lambda: api.post("/api/organisation/leave"))
            leaving_it.join(timeout=0.5)
            assert leaving_it.is_alive(), "the leave should wait for the add"
            go_on.set()
            adding.join(timeout=10)
            leaving_it.join(timeout=10)

        assert added["value"].status_code == 201, added
        assert left["value"].status_code == 200, left
        assert users_in(db, chat) == [member.id]
    finally:
        go_on.set()
        delete_user(db, carol)


def test_the_upgrade_takes_members_who_left_out_of_chats_and_hands_theirs_over(
    api,
    db,
    user,
    member,  # noqa: F811
    outsider,  # noqa: F811
):
    """Revision 0051, on what adding, making or joining a chat as its user left the organisation
    left before: a chat with someone (`outsider`) who left in it, handed to them as its member
    added first when its owner left, and a private one they made as they left."""
    with signed_in_as(member):
        plans = make_chat(api, "Plans", visibility="private")
        theirs = make_chat(api, "Theirs", visibility="private")
    db.add(
        ChatMember(
            chat_id=plans["id"],
            kind=ChatMemberKind.user,
            user_id=outsider.id,
            role=ChatMemberRole.member,
        )
    )
    db.execute(
        update(ChatMember)
        .where(ChatMember.chat_id == theirs["id"], ChatMember.user_id == member.id)
        .values(user_id=outsider.id)
    )
    db.commit()
    with signed_in_as(member):
        add_user(api, plans, user)
        assert api.post("/api/organisation/leave").status_code == 200
    # Handed to the one who left: nobody still here can manage it.
    assert owners(api, plans) == [outsider.id]

    with Operations.context(MigrationContext.configure(db.connection())):
        revision("0051_chat_members_who_left").upgrade()
    db.commit()

    assert users_in(db, plans) == [user.id]
    assert owners(api, plans) == [user.id]
    got = {c["name"]: c["visibility"] for c in api.get("/api/chats").json()}
    assert got == {"Plans": "private", "Theirs": "public"}
    assert api.post(f"/api/chats/{theirs['id']}/join").json()["is_owner"] is True
