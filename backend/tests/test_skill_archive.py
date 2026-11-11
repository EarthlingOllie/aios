"""Archiving a skill: always allowed, even while agents use it.

`POST /api/skills/{id}/archive` and `/unarchive` put it away and bring it back, never refused.
`GET /api/skills/{id}/used-by` lists the agents whose current version uses it. Agent versions
that pin it keep running; it can't be added to a new agent version; an agent whose current
version uses it says so.
"""

import pytest
from test_agents import agent_body, chat_body, make_agent, make_skill
from test_chats import signed_in_as
from test_organisation_scoping import member, outsider  # noqa: F401 (fixtures)

from aios.testing.fake_model import model, use_model


def archive(api, skill: dict) -> dict:
    resp = api.post(f"/api/skills/{skill['id']}/archive")
    assert resp.status_code == 200, resp.text
    return resp.json()


def unarchive(api, skill: dict) -> dict:
    resp = api.post(f"/api/skills/{skill['id']}/unarchive")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_archiving_and_unarchiving_a_skill(api, user):
    skill = make_skill(api, "Pirate", "Talk like a pirate.")
    assert (skill["archived_at"], skill["archived_by"]) == (None, None)

    archived = archive(api, skill)

    assert archived["archived_at"] is not None
    assert archived["archived_by"] == {
        "id": user.id,
        "name": user.full_name,
        "former_member": False,
    }
    # Its page still opens, archived, with its versions.
    assert api.get(f"/api/skills/{skill['id']}").json()["archived_at"] == archived["archived_at"]
    assert api.get(f"/api/skills/{skill['id']}/versions").status_code == 200
    assert archive(api, skill)["archived_at"] == archived["archived_at"]

    back = unarchive(api, skill)

    assert (back["archived_at"], back["archived_by"]) == (None, None)
    make_agent(api, "Bot", skills=[back])


def test_the_skills_list_hides_archived_skills_unless_asked_for_them(api):
    make_skill(api, "Kept")
    put_away = make_skill(api, "Put away")
    archive(api, put_away)

    default = api.get("/api/skills").json()
    archived = api.get("/api/skills", params={"archived": "true"}).json()

    assert [s["name"] for s in default] == ["Kept"]
    assert default[0]["archived_at"] is None
    assert [s["name"] for s in archived] == ["Put away"]
    assert archived[0]["archived_at"] is not None


def test_archiving_is_never_refused_and_used_by_lists_the_agents_using_it(api):
    skill = make_skill(api, "Pirate", "Arr.")
    bot = make_agent(api, "Bot", skills=[skill])
    make_agent(api, "Other")
    moved_on = make_agent(api, "Moved on", skills=[skill])
    api.put(f"/api/agents/{moved_on['id']}", json=agent_body(moved_on, skill_version_ids=[]))

    archive(api, skill)
    resp = api.get(f"/api/skills/{skill['id']}/used-by")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "agents": [
            {"agent_id": bot["id"], "agent_name": "Bot", "agent_version": 1, "skill_version": 1}
        ]
    }


def test_an_agent_pinning_an_archived_skill_still_runs_with_it(api, monkeypatch):
    skill = make_skill(api, "Pirate", "Talk like pirates.")
    agent = make_agent(api, "Bot", base_prompt="Be nice.", skills=[skill])
    archive(api, skill)
    sent = []

    async def script(msgs, info):
        sent.append(msgs)
        yield "arr"

    run = api.post(f"/api/agents/{agent['id']}/runs")
    assert run.status_code == 201, run.text
    with use_model(model(script)):
        resp = api.post(
            f"/api/agents/{agent['id']}/runs/{run.json()['id']}/chat", json=chat_body("hi")
        )

    assert resp.status_code == 200, resp.text
    assert "Talk like pirates." in sent[0][0].content


def test_a_new_agent_version_including_an_archived_skill_is_refused(api):
    skill = make_skill(api, "Pirate", "Arr.")
    agent = make_agent(api, "Bot", skills=[skill])
    archive(api, skill)
    url = f"/api/agents/{agent['id']}"

    # Neither a new agent with it, nor a new version of an agent that already has it.
    new = api.post(
        "/api/agents", json={"name": "New", "skill_version_ids": [skill["current_version"]["id"]]}
    )
    changed = api.put(url, json=agent_body(agent, base_prompt="Changed."))

    for resp in (new, changed):
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "The skill Pirate is archived."
    assert api.get(url).json()["current_version"]["version"] == 1
    # Renaming the agent makes no new version, so it goes through; so does dropping the skill.
    renamed = api.put(url, json=agent_body(agent, name="Bot 2"))
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["current_version"]["version"] == 1
    dropped = api.put(url, json=agent_body(agent, skill_version_ids=[]))
    assert dropped.json()["current_version"]["version"] == 2


def test_the_agent_says_when_a_skill_it_uses_is_archived(api):
    skill = make_skill(api, "Pirate", "Arr.")
    other = make_skill(api, "Terse", "Short.")
    agent = make_agent(api, "Bot", skills=[skill, other])
    archive(api, skill)

    shown = api.get(f"/api/agents/{agent['id']}").json()["current_version"]["skills"]

    assert [(s["name"], s["archived"]) for s in shown] == [("Pirate", True), ("Terse", False)]
    unarchive(api, skill)
    shown = api.get(f"/api/agents/{agent['id']}").json()["current_version"]["skills"]
    assert [s["archived"] for s in shown] == [False, False]


def test_any_member_archives_and_unarchives(api, member):  # noqa: F811
    skill = make_skill(api, "Pirate")

    with signed_in_as(member):
        archived = archive(api, skill)
        assert archived["archived_by"] == {
            "id": member.id,
            "name": member.full_name,
            "former_member": False,
        }
        assert unarchive(api, skill)["archived_at"] is None


@pytest.mark.parametrize("path", ["archive", "unarchive", "used-by"])
def test_another_organisations_skill_isnt_found(api, outsider, path):  # noqa: F811
    skill = make_skill(api, "Pirate")

    with signed_in_as(outsider):
        call = api.get if path == "used-by" else api.post
        assert call(f"/api/skills/{skill['id']}/{path}").status_code == 404


def test_no_route_deletes_a_skill_or_an_agent(api):
    skill = make_skill(api, "Pirate")
    agent = make_agent(api, "Bot")

    assert api.delete(f"/api/skills/{skill['id']}").status_code == 405
    assert api.delete(f"/api/agents/{agent['id']}").status_code == 405
    assert api.get(f"/api/agents/{agent['id']}/dependents").status_code == 404
