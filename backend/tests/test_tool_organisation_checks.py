"""Organisation-aware tool checks (`ToolSpec.check`): a tool whose settings name something of an
organisation's (a connector, say) refuses settings that belong to another organisation when an
agent version is saved, restored or run, naming the tool and the reason. Tools without a check
(every tool in the library today) save, restore and run as before."""

import pytest
from conftest import org_of, take_turn
from fastapi import HTTPException
from pydantic import BaseModel
from test_agents import agent_body, make_agent
from test_workflows import agent_node, make_workflow

from aios.tools import TOOLS, ToolOrganisation, ToolSpec

# Which organisation each "thing" a test tool can be pointed at belongs to, as a connector
# belongs to the organisation that made it.
OWNERS: dict[str, int] = {}


class ThingConfig(BaseModel):
    thing: str = ""


def check_thing(config: ThingConfig, org: ToolOrganisation) -> str | None:
    if OWNERS.get(config.thing) != org.organisation_id:
        return f"{config.thing} belongs to another organisation"
    return None


@pytest.fixture
def thing_tool(monkeypatch):
    spec = ToolSpec(
        key="thing",
        name="Thing poster",
        description="Posts to a thing.",
        config_model=ThingConfig,
        build=lambda config: [],
        check=check_thing,
    )
    monkeypatch.setitem(TOOLS, "thing", spec)
    monkeypatch.setattr(f"{__name__}.OWNERS", {})
    return spec


def thing(name: str) -> dict:
    return {"key": "thing", "config": {"thing": name}}


REFUSAL = "Thing poster can't be used: ours belongs to another organisation"


def test_settings_of_the_organisations_own_save_and_run(api, db, user, thing_tool):
    OWNERS["ours"] = org_of(db, user).id
    agent = make_agent(api, "Poster")
    url = f"/api/agents/{agent['id']}"

    resp = api.put(url, json=agent_body(agent, tools=[thing("ours")]))

    assert resp.status_code == 200, resp.text
    assert resp.json()["current_version"]["tools"][0]["config"] == {"thing": "ours"}
    assert api.post(f"{url}/runs").status_code == 201


def test_another_organisations_settings_are_refused_on_save(api, db, user, thing_tool):
    OWNERS["ours"] = org_of(db, user).id + 1
    agent = make_agent(api, "Poster")

    resp = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[thing("ours")]))

    assert (resp.status_code, resp.json()["detail"]) == (422, REFUSAL)
    created = api.post("/api/agents", json={"name": "Other", "tools": [thing("ours")]})
    assert (created.status_code, created.json()["detail"]) == (422, REFUSAL)
    assert api.get(f"/api/agents/{agent['id']}").json()["current_version"]["version"] == 1


def test_an_unchanged_save_is_checked_again(api, db, user, thing_tool):
    OWNERS["ours"] = org_of(db, user).id
    agent = make_agent(api, "Poster")
    url = f"/api/agents/{agent['id']}"
    agent = api.put(url, json=agent_body(agent, tools=[thing("ours")])).json()
    OWNERS["ours"] = org_of(db, user).id + 1

    resp = api.put(url, json=agent_body(agent, name="Renamed", tools=[thing("ours")]))

    assert (resp.status_code, resp.json()["detail"]) == (422, REFUSAL)


def test_a_version_whose_settings_now_belong_elsewhere_cant_be_restored_or_run(
    api, db, user, thing_tool
):
    OWNERS["ours"] = org_of(db, user).id
    agent = make_agent(api, "Poster")
    url = f"/api/agents/{agent['id']}"
    agent = api.put(url, json=agent_body(agent, tools=[thing("ours")])).json()
    run = api.post(f"{url}/runs").json()
    api.put(url, json=agent_body(agent, tools=[]))
    OWNERS["ours"] = org_of(db, user).id + 1

    restored = api.post(f"{url}/versions/2/restore")
    assert (restored.status_code, restored.json()["detail"]) == (
        409,
        f"That version can't be restored: {REFUSAL}",
    )
    with pytest.raises(HTTPException) as refused:
        take_turn(api, agent["id"], run["id"], "hi")
    assert (refused.value.status_code, refused.value.detail) == (
        409,
        f"This agent version can't be run: {REFUSAL}",
    )


def test_no_new_run_or_workflow_run_with_another_organisations_settings(api, db, user, thing_tool):
    OWNERS["ours"] = org_of(db, user).id
    agent = make_agent(api, "Poster")
    url = f"/api/agents/{agent['id']}"
    agent = api.put(url, json=agent_body(agent, tools=[thing("ours")])).json()
    workflow = make_workflow(api, "Flow", [agent_node("post", agent)])
    OWNERS["ours"] = org_of(db, user).id + 1

    resp = api.post(f"{url}/runs")
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        f"This agent version can't be run: {REFUSAL}",
    )
    resp = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        f"Agent Poster v2 can't run: {REFUSAL}",
    )
