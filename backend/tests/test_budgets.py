"""Budgets: a monthly budget for the organisation, and allowances per role and per person, in
estimated GBP per calendar month in UTC. Running totals are kept with the usage ledger; at
100% new work is refused before any model call, while work already going finishes; admins are
warned at 80% and told at 100%, once a month each.

Through the API, as a signed-in member, with the scripted model and round prices."""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import join_organisation, signed_in_store
from sqlalchemy import update
from test_agents import make_agent
from test_audit_log import outbox_of
from test_chat_agent_invocations import replies_from, say
from test_chats import add, make_chat, messages, post, signed_in_as
from test_event_dispatcher import dispatch, fired, shown
from test_guards import counted_handoffs
from test_more_events import chat_trigger
from test_notifications import unread_of
from test_permissions import PARTICIPANT, set_role
from test_scheduler import make_due, tick
from test_start_workflow import make_flow, starter
from test_triggers import make_trigger, schedule

from aios import budgets
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import SpendTotal
from aios.testing.fake_model import (
    ToolCall,
    Usage,
    instructions,
    last_tool_results,
    model,
    tool_output,
    use_handoff,
    use_model,
)

MODEL = "anthropic:claude-sonnet-5-5"


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    """£2 and £10 per million tokens in and out: a call of 100,000 in and 10,000 out is £0.30."""
    settings = get_settings()
    monkeypatch.setattr(settings, "price_table", {MODEL: {"input": 2.0, "output": 10.0}})
    monkeypatch.setattr(settings, "llm_model", MODEL)


def thirty_pence(text: str = "Done."):
    """A model whose every reply is one call costing £0.30."""

    async def script(messages, info):
        yield Usage(100_000, 10_000)
        yield text

    return model(script)


def budget(api) -> dict:
    resp = api.get("/api/organisation/budget")
    assert resp.status_code == 200, resp.text
    return resp.json()


def set_budget(api, amount, **role_allowances):
    return api.put(
        "/api/organisation/budget", json={"budget": amount, "role_allowances": role_allowances}
    )


@pytest.fixture
def participant(db, user):
    u = join_organisation(db, user, "participant", first_name="Pat", last_name="Participant")
    set_role(db, u, PARTICIPANT, of=user)
    return u


def test_with_no_budget_set_nothing_is_refused_and_spend_is_still_counted(api, user):
    assert budget(api)["budget"] is None
    agent = make_agent(api, "Helper")
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", agent)

    with use_model(thirty_pence()):
        for _ in range(3):
            say(api, chat, "go", ("agent", agent))

    assert replies_from(api, chat) == [("Helper", "Done.")] * 3
    data = budget(api)
    assert data["spent"] == pytest.approx(0.90)
    [me] = data["members"]
    assert (me["user"]["id"], me["spent"]) == (user.id, pytest.approx(0.90))


def test_admins_set_the_budget_and_role_allowances_at_once_and_audited(api, db, user):
    resp = set_budget(api, 25, participant=2.5, editor=10)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["budget"] == 25
    assert data["role_allowances"] == {"admin": None, "editor": 10, "participant": 2.5}
    assert data["warning_threshold"] == 0.8
    assert budget(api)["budget"] == 25

    assert set_budget(api, None, participant=2.5).status_code == 200
    changes = [r.details for r in outbox_of(db, user) if r.action == "settings.changed"]
    assert changes == [
        {"setting": "budget", "from": None, "to": 25},
        {
            "setting": "role_allowances",
            "from": {},
            "to": {"editor": 10, "participant": 2.5},
        },
        {"setting": "budget", "from": 25, "to": None},
        {
            "setting": "role_allowances",
            "from": {"editor": 10, "participant": 2.5},
            "to": {"participant": 2.5},
        },
    ]


def set_allowance(api, person, amount):
    return api.put(f"/api/organisation/members/{person.id}/allowance", json={"allowance": amount})


def test_an_admin_overrides_a_members_allowance_and_only_admins_may(api, db, user, participant):
    set_budget(api, None, participant=2)
    resp = api.put(f"/api/organisation/members/{participant.id}/allowance", json={"allowance": 7})
    assert resp.status_code == 200, resp.text
    pat = next(m for m in resp.json()["members"] if m["user"]["id"] == participant.id)
    assert (pat["allowance"], pat["allowance_in_force"]) == (7, 7)
    [entry] = [r for r in outbox_of(db, user) if r.action == "member.allowance_changed"]
    assert (entry.target_id, entry.details) == (str(participant.id), {"from": None, "to": 7})

    resp = api.put(
        f"/api/organisation/members/{participant.id}/allowance", json={"allowance": None}
    )
    pat = next(m for m in resp.json()["members"] if m["user"]["id"] == participant.id)
    assert (pat["allowance"], pat["allowance_in_force"]) == (None, 2)

    with signed_in_as(participant):
        assert set_budget(api, 1).status_code == 403
        resp = api.put(
            f"/api/organisation/members/{participant.id}/allowance", json={"allowance": 99}
        )
        assert resp.status_code == 403
        # Members read the budget, and only their own spend.
        data = budget(api)
        assert data["everyone"] is False
        assert [m["user"]["id"] for m in data["members"]] == [participant.id]
    assert (
        api.put("/api/organisation/members/999999/allowance", json={"allowance": 1}).status_code
        == 404
    )


@pytest.mark.parametrize("amount", [-1, "lots", float("inf")])
def test_a_budget_must_be_a_sum_of_pounds(api, amount):
    resp = api.put("/api/organisation/budget", content=f'{{"budget": {_json(amount)}}}')
    assert resp.status_code == 422


def _json(value) -> str:
    import json

    return json.dumps(value) if value != float("inf") else "1e999"


def test_at_the_budget_mentions_are_refused_on_their_card_and_call_no_model(api, user):
    agent = make_agent(api, "Helper")
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", agent)
    set_budget(api, 0.5)
    calls: list = []

    async def counted(messages, info):
        calls.append(1)
        yield Usage(100_000, 10_000)
        yield "Done."

    with use_model(model(counted)):
        say(api, chat, "one", ("agent", agent))
        say(api, chat, "two", ("agent", agent))
        posted = say(api, chat, "three", ("agent", agent))

    # £0.60 spent of £0.50: the third was refused, before any model call.
    assert len(calls) == 2
    [invocation] = posted["invocations"]
    assert invocation["status"] == "refused"
    reason = budgets.BUDGET_REACHED.format(amount="0.50")
    assert invocation["refusal_reason"] == reason
    assert posted["messages"][-1]["body"] == reason

    # Raising it takes effect at once.
    set_budget(api, 5)
    with use_model(thirty_pence("Again.")):
        posted = say(api, chat, "four", ("agent", agent))
    assert posted["invocations"][0]["status"] != "refused"


def test_a_participant_at_their_allowance_is_refused_while_others_go_on(api, user, participant):
    agent = make_agent(api, "Helper")
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", agent)
    set_budget(api, 100, participant=0.25)

    with use_model(thirty_pence()), signed_in_as(participant):
        assert api.post(f"/api/chats/{chat['id']}/join").is_success
        say(api, chat, "one", ("agent", agent))
        posted = say(api, chat, "two", ("agent", agent))
    [refused] = posted["invocations"]
    assert refused["status"] == "refused"
    assert refused["refusal_reason"] == budgets.ALLOWANCE_REACHED.format(
        name="Pat Participant", amount="0.25"
    )
    # The admin has no allowance, and the organisation is well within its budget.
    with use_model(thirty_pence()):
        assert say(api, chat, "three", ("agent", agent))["invocations"][0]["status"] != "refused"

    # An admin raises the participant's own allowance: they go on at once.
    api.put(f"/api/organisation/members/{participant.id}/allowance", json={"allowance": 1})
    with use_model(thirty_pence()), signed_in_as(participant):
        assert say(api, chat, "four", ("agent", agent))["invocations"][0]["status"] != "refused"
    pat = next(m for m in budget(api)["members"] if m["user"]["id"] == participant.id)
    assert pat["spent"] == pytest.approx(0.60)


def test_admins_are_warned_at_80_percent_and_told_at_100_once_a_month(api, db, user):
    editor = join_organisation(db, user, "editor")
    agent = make_agent(api, "Helper")
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", agent)
    set_budget(api, 1)

    with use_model(thirty_pence()):
        say(api, chat, "£0.30", ("agent", agent))
        say(api, chat, "£0.60", ("agent", agent))
        assert unread_of(api, "budget_warning") == []
        say(api, chat, "£0.90", ("agent", agent))
        [warning] = unread_of(api, "budget_warning")
        assert warning["ref"] == {"month": budgets.this_month().strftime("%Y-%m")}
        name = api.get("/api/organisation").json()["name"]
        assert warning["title"] == f"{name} has used 80% of its monthly budget"
        assert unread_of(api, "budget_reached") == []
        api.post(f"/api/me/notifications/{warning['id']}/read")
        say(api, chat, "£1.20", ("agent", agent))

    # Once each this month: read or not, the warning isn't repeated.
    assert unread_of(api, "budget_warning") == []
    [reached] = unread_of(api, "budget_reached")
    assert reached["title"] == f"{name} has reached its monthly budget"
    # Only admins.
    with signed_in_as(editor):
        assert unread_of(api) == []


def test_lowering_the_budget_below_this_months_spend_tells_the_admins_at_once(api):
    agent = make_agent(api, "Helper")
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", agent)
    with use_model(thirty_pence()):
        say(api, chat, "go", ("agent", agent))
    set_budget(api, 0.2)
    assert len(unread_of(api, "budget_reached")) == 1


def spend_as(api, person, amount_in_calls: int) -> None:
    """Spend £0.30 a call, as `person` (None: the signed-in admin) mentioning a helper."""
    agent = make_agent(api, f"Spender {datetime.now(UTC).timestamp()}")
    chat = make_chat(api, f"Spending {datetime.now(UTC).timestamp()}")
    add(api, chat, "agent", agent)
    with use_model(thirty_pence()):
        if person is None:
            for _ in range(amount_in_calls):
                say(api, chat, "go", ("agent", agent))
            return
        with signed_in_as(person):
            assert api.post(f"/api/chats/{chat['id']}/join").is_success
            for _ in range(amount_in_calls):
                say(api, chat, "go", ("agent", agent))


def test_a_chat_message_trigger_is_charged_to_whoever_posted_and_refused_at_their_allowance(
    api, user, participant
):
    reader = make_agent(api, "Reader")
    chat = make_chat(api, "Ops")
    t = make_trigger(api, chat_trigger("agent", reader["id"], chat["id"]))
    with signed_in_as(participant):
        assert api.post(f"/api/chats/{chat['id']}/join").is_success
        post(api, chat, "Please read this")
    dispatch(thirty_pence())
    [invocation] = fired(t["id"])
    assert invocation.charged_to == participant.id
    pat = next(m for m in budget(api)["members"] if m["user"]["id"] == participant.id)
    assert pat["spent"] == pytest.approx(0.30)

    set_budget(api, None, participant=0.3)
    with signed_in_as(participant):
        post(api, chat, "And this")
    dispatch(thirty_pence())
    assert len(fired(t["id"])) == 1
    seen = shown(api, t["id"])
    assert (seen["last_status"], seen["last_error"]) == (
        "refused",
        budgets.ALLOWANCE_REACHED.format(name="Pat Participant", amount="0.30"),
    )
    # The admin, with no allowance, still fires it.
    post(api, chat, "From the admin")
    dispatch(thirty_pence())
    assert len(fired(t["id"])) == 2


def test_a_schedule_counts_only_against_the_organisations_budget(api, db, user, participant):
    spend_as(api, None, 1)
    set_budget(api, 0.5, admin=0.01, participant=0.01)
    agent = make_agent(api, "Newsbot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    with use_model(thirty_pence()):
        assert tick(now) == [t["id"]]
    # Its run charged nobody's allowance, though both are past theirs.
    assert shown(api, t["id"])["last_status"] == "succeeded"
    assert budget(api)["spent"] == pytest.approx(0.60)
    assert {m["user"]["id"]: m["spent"] for m in budget(api)["members"]} == {
        user.id: pytest.approx(0.30),
        participant.id: 0,
    }

    later = now + timedelta(days=1)
    make_due(db, t["id"], later - timedelta(seconds=1))
    with use_model(thirty_pence()):
        assert tick(later) == [t["id"]]
    seen = shown(api, t["id"])
    assert (seen["last_status"], seen["last_error"]) == (
        "refused",
        budgets.BUDGET_REACHED.format(amount="0.50"),
    )
    assert budget(api)["spent"] == pytest.approx(0.60)


def test_start_workflow_is_refused_as_its_result_while_the_calling_turn_finishes(api, user):
    summariser = make_agent(api, "Summariser", base_prompt="Summariser")
    flow = make_flow(api, "Summarise", summariser)
    planner = starter(api, flow)
    chat = make_chat(api, "Design")
    add(api, chat, "agent", planner)
    set_budget(api, 0.25)
    results: list = []

    async def script(msgs, info):
        if instructions(msgs) != "Planner":
            yield "Summary."
            return
        results.extend(tool_output(r) for r in last_tool_results(msgs))
        # Each of the turn's calls costs £0.30: the first passes the budget.
        yield Usage(100_000, 10_000)
        if not results:
            yield ToolCall("start_workflow", {"workflow": flow["id"], "input": "Plan it"})
        else:
            yield "Couldn't start it."

    with use_model(model(script)):
        say(api, chat, "go", ("agent", planner))

    assert results == [{"error": budgets.BUDGET_REACHED.format(amount="0.25")}]
    # The turn already going finished, its second call past the budget included.
    assert replies_from(api, chat) == [("Planner", "Couldn't start it.")]
    assert budget(api)["spent"] == pytest.approx(0.60)
    assert api.get(f"/api/workflows/{flow['id']}/runs").json() == []


def test_a_workflow_an_agent_starts_is_charged_to_the_person_who_mentioned_the_agent(
    api, user, participant
):
    summariser = make_agent(api, "Summariser", base_prompt="Summariser")
    flow = make_flow(api, "Summarise", summariser)
    planner = starter(api, flow)
    chat = make_chat(api, "Design")
    add(api, chat, "agent", planner)

    async def script(msgs, info):
        if instructions(msgs) != "Planner":
            # The started workflow's node: £0.30.
            yield Usage(100_000, 10_000)
            yield "Summary."
        elif not last_tool_results(msgs):
            yield ToolCall("start_workflow", {"workflow": flow["id"], "input": "Plan it"})
        else:
            yield "Started."

    with use_model(model(script)), signed_in_as(participant):
        assert api.post(f"/api/chats/{chat['id']}/join").is_success
        say(api, chat, "go", ("agent", planner))

    [run] = api.get(f"/api/workflows/{flow['id']}/runs").json()
    assert run["status"] == "succeeded"
    spends = {m["user"]["id"]: m["spent"] for m in budget(api)["members"]}
    assert spends == {user.id: 0, participant.id: pytest.approx(0.30)}


def test_starting_a_workflow_by_hand_at_the_budget_is_a_409(api):
    summariser = make_agent(api, "Summariser")
    flow = make_flow(api, "Summarise", summariser)
    spend_as(api, None, 1)
    set_budget(api, 0.3)
    resp = api.post(f"/api/workflows/{flow['id']}/runs", json={"input": "Go"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == budgets.BUDGET_REACHED.format(amount="0.30")


def test_the_nightly_reconciliation_puts_drifted_totals_right_against_the_ledger(api, user):
    spend_as(api, None, 2)
    store_id = signed_in_store()
    month = budgets.this_month()
    with store_session(store_id) as store:
        store.execute(
            update(SpendTotal).where(SpendTotal.month == month).values(cost=SpendTotal.cost + 5)
        )
        store.commit()
    assert budget(api)["spent"] == pytest.approx(5.60)

    drift = budgets.reconcile(store_id)

    assert drift == {
        budgets.ORGANISATION: pytest.approx(-5),
        user.id: pytest.approx(-5),
    }
    assert budget(api)["spent"] == pytest.approx(0.60)
    assert budgets.reconcile(store_id) == {}


def test_a_mention_queued_as_the_budget_is_reached_is_refused_before_its_handoff(api, run_service):
    agent = make_agent(api, "Helper")
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", agent)
    run_service.auto = False
    [queued] = say(api, chat, "one", ("agent", agent))["invocations"]
    assert queued["status"] == "queued"
    # An admin sets a budget of nothing before the run service gets to it.
    set_budget(api, 0)
    handoffs: list = []

    with use_handoff(counted_handoffs(handoffs)), use_model(thirty_pence()):
        run_service.drain()

    assert handoffs == []
    [card] = [m for m in messages(api, chat)["messages"] if m["kind"] == "run_card"]
    assert card["body"] == budgets.BUDGET_REACHED.format(amount="0.00")
    assert replies_from(api, chat) == []
    assert budget(api)["spent"] == 0


def test_mentions_queued_behind_one_refused_at_the_budget_are_refused_in_turn(api, run_service):
    """Continuing runs queue an agent's later mentions in a chat behind its earlier one; when
    that one is refused at the budget, the ones behind it come up and are refused too, rather
    than waiting forever."""
    agent = make_agent(api, "Helper")
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", agent)
    run_service.auto = False
    for word in ("one", "two"):
        say(api, chat, word, ("agent", agent))
    set_budget(api, 0)
    handoffs: list = []

    with use_handoff(counted_handoffs(handoffs)), use_model(thirty_pence()):
        run_service.drain()

    assert handoffs == []
    cards = [m for m in messages(api, chat)["messages"] if m["kind"] == "run_card"]
    assert [c["body"] for c in cards] == [budgets.BUDGET_REACHED.format(amount="0.00")] * 2
    assert [i["status"] for i in messages(api, chat)["invocations"]] == ["refused", "refused"]
    assert replies_from(api, chat) == []


def test_the_totals_are_reconciled_nightly_on_the_routine_worker():
    from aios import worker

    entry = worker.celery_app.conf.beat_schedule["reconcile-spend-totals"]
    assert entry["task"] == "aios.reconcile_spend_totals"
    assert worker.celery_app.conf.task_routes[entry["task"]] == {"queue": "routine"}
