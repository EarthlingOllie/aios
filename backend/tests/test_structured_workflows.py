"""Structured workflows through the API: schemas, templates and conditions saved and checked,
runs refused on bad input, structured output, branching and skips, and resuming."""

import json

import pytest
from conftest import capture_runs, drain_run, store_of
from test_workflows import agent_node, make_agent, workflow_body

from aios import jev
from aios.testing.fake_model import ToolCall, instructions, model, use_model

SCORE = {
    "type": "object",
    "properties": {"score": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["score"],
}
TOPIC = {
    "type": "object",
    "properties": {"topic": {"type": "string"}, "urgent": {"type": "boolean"}},
    "required": ["topic"],
}


@pytest.fixture
def queued(monkeypatch, user):
    return capture_runs(monkeypatch, store_of(user))


def agents(api, *names: str) -> dict[str, dict]:
    return {name: make_agent(api, name, base_prompt=name) for name in names}


def node(key: str, agent: dict, **extra) -> dict:
    return {**agent_node(key, agent), **extra}


def edge(source: str, target: str, condition=None) -> dict:
    return {"source": source, "target": target, "condition": condition}


def body(name: str, nodes, edges=(), **extra) -> dict:
    return {**workflow_body(name, nodes), "edges": list(edges), **extra}


def save(api, payload: dict, workflow_id: int | None = None):
    if workflow_id is None:
        return api.post("/api/workflows", json=payload)
    return api.put(f"/api/workflows/{workflow_id}", json=payload)


def refused(resp) -> list[dict]:
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail["message"], str) and detail["message"]
    return detail["errors"]


# --- Saving -------------------------------------------------------------------------------------


def test_schemas_templates_and_conditions_are_saved_and_versioned(api):
    a = agents(api, "scorer", "writer")
    nodes = [
        node("score", a["scorer"], output_schema=SCORE, input_template="Rate {{input.topic}}"),
        node("write", a["writer"], input_template={"why": "{{nodes.score.reason}}"}),
    ]
    edges = [edge("score", "write", {">": [{"var": "output.score"}, 5]})]
    payload = body("rate", nodes, edges, input_schema=TOPIC, output_schema={"type": "string"})
    resp = save(api, payload)
    assert resp.status_code == 201, resp.text
    version = resp.json()["current_version"]
    assert (version["input_schema"], version["output_schema"]) == (TOPIC, {"type": "string"})
    score, write = version["nodes"]
    assert (score["output_schema"], score["input_template"]) == (SCORE, "Rate {{input.topic}}")
    assert (score["input_schema"], write["output_schema"]) == (None, None)
    assert write["input_template"] == {"why": "{{nodes.score.reason}}"}
    assert version["edges"] == [
        {"source": "score", "target": "write", "condition": {">": [{"var": "output.score"}, 5]}}
    ]

    wid = resp.json()["id"]
    # The same again is no new version; a changed condition is.
    assert save(api, payload, wid).json()["current_version"]["version"] == 1
    edges[0]["condition"] = {">": [{"var": "output.score"}, 6]}
    assert (
        save(api, body("rate", nodes, edges, input_schema=TOPIC), wid).json()["current_version"][
            "version"
        ]
        == 2
    )


def test_a_workflow_without_any_has_none(api):
    a = agents(api, "one")
    resp = save(api, body("plain", [node("one", a["one"])]))
    version = resp.json()["current_version"]
    assert (version["input_schema"], version["output_schema"]) == (None, None)
    assert version["nodes"][0]["input_template"] is None


def test_saving_refuses_invalid_schemas_templates_and_conditions_per_item(api):
    a = agents(api, "first", "second", "third")
    nodes = [
        node("first", a["first"], output_schema={"type": "nonsense"}),
        node("second", a["second"], input_template="{{nodes.third.x}} and {{1 + 2}}"),
        node("third", a["third"]),
    ]
    edges = [
        edge("first", "second", {"+": [1, 2]}),
        edge("second", "third", {"var": "output.anything"}),
    ]
    errors = refused(save(api, body("bad", nodes, edges, input_schema=[1])))
    items: dict[str, str] = {}
    for e in errors:
        items[e["item"]] = items.get(e["item"], "") + e["message"] + "\n"
    assert set(items) >= {
        "input_schema",
        "nodes.first.output_schema",
        "nodes.second.input_template",
        "edges.first→second.condition",
        "edges.second→third.condition",
    }
    assert "not a predecessor" in items["nodes.second.input_template"]
    assert "isn't a placeholder" in items["nodes.second.input_template"]
    assert "not allowed" in items["edges.first→second.condition"]
    assert "no output schema" in items["edges.second→third.condition"]


def test_conditions_may_only_read_fields_the_schemas_declare(api):
    a = agents(api, "scorer", "writer")
    nodes = [node("score", a["scorer"], output_schema=SCORE), node("write", a["writer"])]
    typo = [edge("score", "write", {">": [{"var": "output.scor"}, 5]})]
    [error] = refused(save(api, body("typo", nodes, typo, input_schema=TOPIC)))
    assert "output.scor" in error["message"]
    by_input = [edge("score", "write", {"==": [{"var": "input.region"}, "EU"]})]
    [error] = refused(save(api, body("typo", nodes, by_input, input_schema=TOPIC)))
    assert "input.region" in error["message"]
    big = {"or": [{"==": [{"var": "output.score"}, n]} for n in range(300)]}
    [error] = refused(save(api, body("big", nodes, [edge("score", "write", big)])))
    assert "too large" in error["message"]


# --- Run input ----------------------------------------------------------------------------------


def echo():
    """Agents answer `<name>(<message>)`, with what came wrapped as untrusted shown as such."""

    async def script(messages, info):
        yield f"{instructions(messages)}({messages[-1].content})"

    return model(script)


def start(api, workflow: dict, **payload):
    return api.post(f"/api/workflows/{workflow['id']}/runs", json=payload)


def test_a_run_is_refused_when_its_input_doesnt_fit_the_schema(api, queued):
    a = agents(api, "one")
    wf = save(api, body("typed", [node("one", a["one"])], input_schema=TOPIC)).json()

    errors = refused(start(api, wf, input_json={"urgent": "yes"}))
    assert errors == [
        {"path": "/", "message": "'topic' is a required property"},
        {"path": "/urgent", "message": "'yes' is not of type 'boolean'"},
    ]
    # Text that isn't JSON is the string itself, which isn't an object.
    [error] = refused(start(api, wf, input="hello"))
    assert error["path"] == "/" and "not of type 'object'" in error["message"]
    assert queued == []

    # Text that is JSON is read as JSON.
    resp = start(api, wf, input='{"topic": "tides"}')
    assert resp.status_code == 201, resp.text
    resp = start(api, wf, input_json={"topic": "moons", "urgent": True})
    assert resp.status_code == 201, resp.text
    started = resp.json()
    assert started["input_json"] == {"topic": "moons", "urgent": True}
    assert started["input"] == '{"topic":"moons","urgent":true}'
    with use_model(echo()):
        drain_run(queued.store, started["id"])
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    # A start node gets the input as JSON text.
    assert out["output"] == 'one({"topic":"moons","urgent":true})'


def test_a_run_without_a_schema_takes_text_as_today(api, queued):
    a = agents(api, "one")
    wf = save(api, body("plain", [node("one", a["one"])])).json()
    started = start(api, wf, input="hello").json()
    assert (started["input"], started["input_json"]) == ("hello", None)
    assert start(api, wf).status_code == 422


MENTION = {
    "type": "object",
    "properties": {"brief": {"type": "string"}, "message": {"type": "string"}},
    "required": ["message"],
}


def test_a_mention_passes_the_brief_and_message_as_an_object_if_the_schema_takes_it(api, queued):
    from test_chat_invocations import say
    from test_chats import add, make_chat, messages

    a = agents(api, "one")
    takes = save(api, body("Takes", [node("one", a["one"])], input_schema=MENTION)).json()
    wont = save(api, body("Wont", [node("one", a["one"])], input_schema=TOPIC)).json()
    chat = make_chat(api, "Design")
    add(api, chat, "workflow", takes)
    add(api, chat, "workflow", wont)

    posted = say(api, chat, "Go", takes, wont)
    [run_id] = queued
    run = api.get(f"/api/workflows/{takes['id']}/runs/{run_id}").json()
    assert run["input_json"] == {"brief": "", "message": jev.wrap("Go", "chat message")}
    by_id = {i["id"]: i for i in messages(api, chat)["invocations"]}
    refusal = by_id[posted["messages"][2]["invocation_id"]]
    assert refusal["status"] == "failed"
    assert refusal["error"].startswith("This workflow's input schema doesn't accept a brief")
    assert "'topic' is a required property" in refusal["error"]


# --- Running ------------------------------------------------------------------------------------


class Script:
    """The model. Each agent (by base prompt) answers from `answers`: a list of what it gives on
    each call, a structured output (a dict, given as the output tool's call) or text; agents
    not in it echo `<name>(<message>)`. Records each agent's messages."""

    def __init__(self, **answers):
        self.answers = {name: list(given) for name, given in answers.items()}
        self.seen: dict[str, list] = {}

    async def __call__(self, messages, info):
        name = instructions(messages)
        self.seen.setdefault(name, []).append(messages)
        if name not in self.answers:
            yield f"{name}({messages[-1].content})"
            return
        answer = self.answers[name].pop(0)
        if isinstance(answer, dict):
            yield ToolCall("workflow_output", answer)
        else:
            yield answer


def run(api, queued, payload: dict, script: Script, **start_payload) -> dict:
    resp = save(api, payload)
    assert resp.status_code == 201, resp.text
    wf = resp.json()
    resp = start(api, wf, **(start_payload or {"input": "x"}))
    assert resp.status_code == 201, resp.text
    with use_model(model(script)):
        drain_run(queued.store, resp.json()["id"])
    return api.get(f"/api/workflows/{wf['id']}/runs/{resp.json()['id']}").json()


def steps(out: dict) -> dict[str, dict]:
    return {"/".join(s["path"]): s for s in out["steps"]}


def test_structured_output_is_validated_with_one_retry_and_passed_on_wrapped(api, queued):
    a = agents(api, "scorer", "writer")
    nodes = [
        node("score", a["scorer"], output_schema=SCORE),
        node("write", a["writer"], input_template="Explain: {{nodes.score.reason}}"),
    ]
    script = Script(scorer=[{"score": "high"}, {"score": 8, "reason": "solid"}])
    out = run(api, queued, body("rate", nodes, [edge("score", "write")]), script)
    assert out["status"] == "succeeded", out["error"]
    score, write = steps(out)["score"], steps(out)["write"]
    assert score["output_json"] == {"score": 8, "reason": "solid"}
    assert score["output"] == '{"score":8,"reason":"solid"}'
    # The retry showed the model what was wrong, in the same turn.
    first, retry = script.seen["scorer"]
    assert "/score" in retry[-1].content and "'high' is not of type 'number'" in retry[-1].content
    assert len(retry) > len(first)
    # The writer's input is the template's; its model got the field wrapped as untrusted.
    assert write["input"] == "Explain: solid"
    [seen] = script.seen["writer"]
    wrapped = jev.wrap("solid", "derived:workflow:score")
    assert seen[-1].content == f"Explain: {wrapped}"


def test_structured_output_that_fails_twice_fails_the_step_with_path_and_message(api, queued):
    a = agents(api, "scorer")
    script = Script(scorer=[{"score": "high"}, {"reason": "no score"}])
    out = run(api, queued, body("rate", [node("score", a["scorer"], output_schema=SCORE)]), script)
    assert out["status"] == "failed"
    error = steps(out)["score"]["error"]
    assert "at /: 'score' is a required property" in error
    assert out["error"] == f"score: {error}"


def test_a_default_input_passes_outputs_on_wrapped_as_untrusted(api, queued):
    a = agents(api, "first", "second")
    nodes = [node("one", a["first"]), node("two", a["second"])]
    script = Script()
    out = run(api, queued, body("chain", nodes, [edge("one", "two")]), script, input="hi")
    assert out["status"] == "succeeded", out["error"]
    assert steps(out)["two"]["input"] == "first(hi)"
    [seen] = script.seen["second"]
    assert seen[-1].content == jev.wrap("first(hi)", "derived:workflow:one")


def branching(api, *, high: str = "escalate", low: str = "archive") -> dict:
    """score → escalate (score > 5) or archive (otherwise) → notify (fed by both)."""
    a = agents(api, "scorer", "escalate", "archive", "notify")
    nodes = [
        node("score", a["scorer"], output_schema=SCORE),
        node(high, a["escalate"]),
        node(low, a["archive"]),
        node("notify", a["notify"]),
    ]
    edges = [
        edge("score", high, {">": [{"var": "output.score"}, 5]}),
        edge("score", low, {"<=": [{"var": "output.score"}, 5]}),
        edge(high, "notify"),
        edge(low, "notify"),
    ]
    return body("triage", nodes, edges)


def test_conditions_branch_and_a_join_runs_with_only_the_active_branch(api, queued):
    script = Script(scorer=[{"score": 9}])
    out = run(api, queued, branching(api), script)
    assert out["status"] == "succeeded", out["error"]
    by_path = steps(out)
    assert by_path["archive"]["status"] == "skipped"
    assert by_path["archive"]["agent_run_id"] is None
    assert by_path["escalate"]["status"] == "succeeded"
    # Escalate's model got the score wrapped; notify gets escalate's output alone.
    escalated = f"escalate({jev.wrap('{"score":9}', 'derived:workflow:score')})"
    assert by_path["notify"]["input"] == escalated
    assert out["output"] == f"notify({jev.wrap(escalated, 'derived:workflow:escalate')})"
    assert "archive" not in script.seen
    assert by_path["score"]["conditions"] == {"escalate": True, "archive": False}


def test_skips_propagate_and_a_run_where_no_final_step_ran_succeeds_without_output(api, queued):
    a = agents(api, "first", "second", "third")
    nodes = [node("one", a["first"]), node("two", a["second"]), node("three", a["third"])]
    never = {"==": [{"var": "output"}, "never"]}
    edges = [edge("one", "two", never), edge("two", "three")]
    out = run(api, queued, body("dud", nodes, edges), Script())
    assert out["status"] == "succeeded", out["error"]
    assert [(s["path"], s["status"]) for s in out["steps"]] == [
        (["one"], "succeeded"),
        (["two"], "skipped"),
        (["three"], "skipped"),
    ]
    assert (out["output"], out["output_json"]) == (None, None)
    assert out["output_note"] == "No final step ran"


def test_one_final_node_skipped_leaves_the_other_as_the_output(api, queued):
    a = agents(api, "scorer", "yes", "no")
    nodes = [
        node("score", a["scorer"], output_schema=SCORE),
        node("yes", a["yes"], output_schema=SCORE),
        node("no", a["no"]),
    ]
    edges = [
        edge("score", "yes", {">": [{"var": "output.score"}, 5]}),
        edge("score", "no", {"<=": [{"var": "output.score"}, 5]}),
    ]
    script = Script(scorer=[{"score": 9}], yes=[{"score": 10}])
    out = run(api, queued, body("pick", nodes, edges, output_schema=SCORE), script)
    assert out["status"] == "succeeded", out["error"]
    assert out["output_json"] == {"score": 10}
    assert out["output"] == '{"score":10}'
    assert steps(out)["no"]["status"] == "skipped"


def test_several_final_nodes_give_an_object_by_node_checked_against_the_output_schema(api, queued):
    a = agents(api, "left", "right")
    nodes = [node("left", a["left"], output_schema=SCORE), node("right", a["right"])]
    script = Script(left=[{"score": 1}])
    out = run(api, queued, body("both", nodes), script)
    assert out["status"] == "succeeded", out["error"]
    assert out["output_json"] == {"left": {"score": 1}, "right": "right(x)"}
    # Text output is as it always was: the final nodes' outputs, joined.
    assert out["output"] == '{"score":1}\n\nright(x)'

    wants_number = {"type": "number"}
    script = Script(left=[{"score": 1}])
    out = run(api, queued, body("both2", nodes, output_schema=wants_number), script)
    assert out["status"] == "failed"
    assert out["error"].startswith("The output doesn't fit the workflow's output schema")


def test_a_condition_that_errors_fails_the_run_naming_the_edge(api, queued):
    a = agents(api, "scorer", "next")
    nodes = [node("score", a["scorer"], output_schema=SCORE), node("next", a["next"])]
    # `reason` is a string, which can't be ordered against a number.
    edges = [edge("score", "next", {">": [{"var": "output.reason"}, 5]})]
    script = Script(scorer=[{"score": 1, "reason": "dunno"}])
    out = run(api, queued, body("broken", nodes, edges), script)
    assert out["status"] == "failed"
    assert "score → next" in out["error"]
    assert "dunno" in out["error"]
    assert "next" not in steps(out)


def test_a_template_field_missing_at_run_time_fails_the_step_naming_the_path(api, queued):
    a = agents(api, "scorer", "writer")
    nodes = [
        node("score", a["scorer"], output_schema=SCORE),
        node("write", a["writer"], input_template="Why: {{nodes.score.reason}}"),
    ]
    script = Script(scorer=[{"score": 3}])
    out = run(api, queued, body("why", nodes, [edge("score", "write")]), script)
    assert out["status"] == "failed"
    write = steps(out)["write"]
    assert write["status"] == "failed" and write["agent_run_id"] is None
    assert "nodes.score.reason" in write["error"]
    assert "writer" not in script.seen


def test_templates_read_the_run_input_and_a_node_checks_its_input_schema(api, queued):
    a = agents(api, "one", "two")
    nodes = [
        node(
            "one", a["one"], input_template={"about": "{{input.topic}}", "rush": "{{input.urgent}}"}
        ),
        node("two", a["two"], input_schema={"type": "number"}),
    ]
    out = run(
        api,
        queued,
        body("typed", nodes, [edge("one", "two")], input_schema=TOPIC),
        Script(),
        input_json={"topic": "tides", "urgent": True},
    )
    assert out["status"] == "failed"
    by_path = steps(out)
    assert json.loads(by_path["one"]["input"]) == {"about": "tides", "rush": True}
    assert by_path["one"]["input_json"] == {"about": "tides", "rush": True}
    assert by_path["one"]["output"] == f"one({by_path['one']['input']})"
    assert "doesn't fit the node's input schema" in by_path["two"]["error"]


def test_a_schedules_input_is_checked_against_the_schema_too(api, db, queued):
    from datetime import UTC, datetime, timedelta

    from test_scheduler import load, make_due, make_trigger, schedule, tick

    from aios.models import TriggerStatus

    a = agents(api, "one")
    wf = save(api, body("typed", [node("one", a["one"])], input_schema=TOPIC)).json()
    bad = make_trigger(api, schedule("workflow", wf["id"], input="not json"))
    good = make_trigger(api, schedule("workflow", wf["id"], input='{"topic": "tides"}'))
    now = datetime.now(UTC)
    for t in (bad, good):
        make_due(db, t["id"], now - timedelta(seconds=1))
    tick(now)
    refused_run = load(db, bad["id"])
    assert refused_run.last_status == TriggerStatus.failed
    assert refused_run.last_error.startswith("The input doesn't fit the workflow's input schema")
    assert refused_run.last_workflow_run_id is None
    started = load(db, good["id"])
    assert started.last_error is None and started.last_workflow_run_id == queued[-1]
    run = api.get(f"/api/workflows/{wf['id']}/runs/{queued[-1]}").json()
    assert run["input_json"] == {"topic": "tides"}


def test_schemas_templates_and_conditions_survive_export_and_import(api):
    from test_exports import export, take, upload

    a = agents(api, "scorer", "writer")
    nodes = [
        node("score", a["scorer"], output_schema=SCORE),
        node("write", a["writer"], input_template="Why: {{nodes.score.reason}}"),
    ]
    condition = {">": [{"var": "output.score"}, 5]}
    wf = save(api, body("rate", nodes, [edge("score", "write", condition)], input_schema=TOPIC))
    file = export(api, "workflow", wf.json())
    imported = take(api, file)
    copy = api.get(f"/api/workflows/{imported['root']['id']}").json()["current_version"]
    assert copy["input_schema"] == TOPIC
    assert copy["nodes"][0]["output_schema"] == SCORE
    assert copy["nodes"][1]["input_template"] == "Why: {{nodes.score.reason}}"
    assert copy["edges"][0]["condition"] == condition

    # A file whose condition the API would refuse isn't an export.
    [entry] = [w for w in file["workflows"].values() if w["name"] == "rate"]
    entry["edges"][0]["condition"] = {"+": [1, 2]}
    resp = upload(api, file)
    assert resp.status_code == 422 and "not allowed" in resp.text
