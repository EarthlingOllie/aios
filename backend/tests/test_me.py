"""The signed-in user's own account: `/api/me`."""

import pytest
from conftest import signed_in_client


def test_editing_both_names(user):
    client = signed_in_client(user)
    resp = client.patch("/api/me", json={"first_name": "Alice", "last_name": "Smith"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "id": user.id,
        "first_name": "Alice",
        "last_name": "Smith",
        "email": user.email,
    }
    assert client.get("/api/me").json() == resp.json()


@pytest.mark.parametrize(
    ("change", "after"),
    [
        ({"first_name": "Alice"}, ("Alice", "User")),
        ({"last_name": "Smith"}, ("Test", "Smith")),
        ({}, ("Test", "User")),
    ],
)
def test_a_name_left_out_is_kept(user, change, after):
    client = signed_in_client(user)
    resp = client.patch("/api/me", json=change)
    assert resp.status_code == 200, resp.text
    me = client.get("/api/me").json()
    assert (me["first_name"], me["last_name"]) == after


def test_names_are_trimmed(user):
    client = signed_in_client(user)
    resp = client.patch("/api/me", json={"first_name": "  Alice ", "last_name": " Smith  "})
    assert resp.status_code == 200, resp.text
    me = client.get("/api/me").json()
    assert (me["first_name"], me["last_name"]) == ("Alice", "Smith")


@pytest.mark.parametrize("field", ["first_name", "last_name"])
@pytest.mark.parametrize("value", ["", "   ", "x" * 101, None])
def test_a_bad_name_is_refused_next_to_its_field(user, field, value):
    client = signed_in_client(user)
    resp = client.patch("/api/me", json={field: value})
    assert resp.status_code == 422, resp.text
    assert [e["loc"] for e in resp.json()["detail"]] == [["body", field]]
    me = client.get("/api/me").json()
    assert (me["first_name"], me["last_name"]) == ("Test", "User")


def test_a_name_of_100_characters_is_accepted(user):
    client = signed_in_client(user)
    resp = client.patch("/api/me", json={"last_name": "x" * 100})
    assert resp.status_code == 200, resp.text
    assert resp.json()["last_name"] == "x" * 100


def test_editing_needs_a_session(client):
    assert client.patch("/api/me", json={"first_name": "Alice"}).status_code == 401


def test_editing_keeps_the_session(user):
    client = signed_in_client(user)
    assert client.patch("/api/me", json={"first_name": "Alice"}).status_code == 200
    other = signed_in_client(user)
    assert other.get("/api/me").status_code == 200


def test_chats_show_the_new_name(user, files_bucket):
    client = signed_in_client(user)
    chat = client.post("/api/chats", json={"name": "Renames"}).json()
    resp = client.post(f"/api/chats/{chat['id']}/messages", json={"body": "Hi"})
    assert resp.status_code == 201, resp.text

    resp = client.patch("/api/me", json={"first_name": "Alice", "last_name": "Smith"})
    assert resp.status_code == 200, resp.text

    members = client.get(f"/api/chats/{chat['id']}").json()["members"]["users"]
    assert [m["name"] for m in members] == ["Alice Smith"]
    [message] = client.get(f"/api/chats/{chat['id']}/messages").json()["messages"]
    assert message["author"]["name"] == "Alice Smith"
