"""Prompted charts and saved charts (`aios.charts`, `aios.api.insights`): a member asks for a
chart in words, the built-in insights agent answers with a title, one SQL statement and a
Vega-Lite spec, the SQL runs as the store's insights role through the capped read-only path,
and the spec is checked against the allowlist. Saved charts re-run their SQL when viewed."""

import pytest
from conftest import join_organisation, rows_of, store_of
from sqlalchemy import select
from test_budgets import set_budget
from test_chats import signed_in_as
from test_data_tools import as_owner, provisioned
from test_permissions import PARTICIPANT, set_role

from aios import budgets, charts, insights
from aios.config import get_settings, libpq_conninfo
from aios.datastores.connections import dispose_engines
from aios.datastores.models import Agent, AuditEntry, Invocation, UsageEntry
from aios.testing.fake_model import ToolCall, Usage, model, use_model

MODEL = "anthropic:claude-sonnet-5-5"


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    """£2 and £10 per million tokens in and out: a call of 100,000 in and 10,000 out is £0.30."""
    settings = get_settings()
    monkeypatch.setattr(settings, "price_table", {MODEL: {"input": 2.0, "output": 10.0}})
    monkeypatch.setattr(settings, "llm_model", MODEL)


@pytest.fixture(autouse=True)
def fresh_cache():
    insights.clear_cache()
    yield
    insights.clear_cache()


@pytest.fixture
def fish(db, user):
    """The user's store, provisioned, with a `data.fish` table."""
    row = provisioned(db, store_of(user))
    with as_owner(row) as conn:
        conn.execute("CREATE TABLE data.fish (name text, weight_kg numeric)")
        conn.execute("INSERT INTO data.fish VALUES ('pike', 4.2), ('perch', 0.8), ('carp', 7.5)")
    return row


def test_the_insights_role_reads_the_insights_views_and_data_tables(fish):
    rows = charts.run_query(fish.id, "SELECT name, weight_kg FROM data.fish ORDER BY name")
    assert rows.columns == ["name", "weight_kg"]
    assert [r[0] for r in rows.rows] == ["carp", "perch", "pike"]
    assert charts.run_query(fish.id, "SELECT count(*) FROM insights.turns").rows == [[0]]


@pytest.mark.parametrize(
    "sql",
    [
        # Conversation content, memory and synced text.
        "SELECT body FROM aios.chat_messages",
        "SELECT * FROM aios.memories",
        "SELECT text FROM aios.synced_items",
        "SELECT brief FROM aios.invocations",
        # Any of the store's base tables, unqualified too (the search path finds `aios`).
        "SELECT * FROM aios.agents",
        "SELECT * FROM agents",
        "SELECT * FROM aios.people",
        "SELECT * FROM aios.insight_charts",
        "SELECT * FROM langgraph.checkpoints",
        # The reporting views agents read, which hold more than metrics.
        "SELECT * FROM reporting.turns",
        "SELECT * FROM reporting.audit_log",
    ],
)
def test_the_insights_role_reads_no_content_and_no_base_table(fish, sql):
    with pytest.raises(charts.ChartQueryError) as refused:
        charts.run_query(fish.id, sql)
    assert "permission denied" in str(refused.value) or "does not exist" in str(refused.value)


def test_the_insights_role_writes_nothing(fish):
    for sql in (
        "INSERT INTO data.fish VALUES ('eel', 1)",
        "DELETE FROM data.fish",
        "CREATE TABLE data.mine (id int)",
        "WITH gone AS (DELETE FROM data.fish RETURNING *) SELECT count(*) FROM gone",
    ):
        with pytest.raises(charts.ChartQueryError):
            charts.run_query(fish.id, sql)
    assert len(charts.run_query(fish.id, "SELECT * FROM data.fish").rows) == 3


def test_the_insights_role_reaches_no_other_store(db, fish, new_user):
    import psycopg

    from aios.datastores import credentials

    elsewhere = provisioned(db, store_of(new_user()))
    with as_owner(elsewhere) as conn:
        conn.execute("CREATE TABLE data.secrets (code text)")
    password = credentials.decrypt(fish.insights_password)
    url = get_settings().database_url
    for database in (elsewhere.dbname, "postgres"):
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(
                libpq_conninfo(url, database=database),
                user=f"{fish.role_prefix}_insights",
                password=password,
            ).close()
    with pytest.raises(charts.ChartQueryError):
        charts.run_query(fish.id, f"SELECT * FROM {elsewhere.dbname}.data.secrets")
    dispose_engines()


# --- Caps ------------------------------------------------------------------------------------


def test_a_chart_query_stops_at_the_row_cap_and_says_so(fish, monkeypatch):
    monkeypatch.setattr(get_settings(), "chart_max_rows", 2)
    rows = charts.run_query(fish.id, "SELECT * FROM generate_series(1, 10) AS n")
    assert (rows.rows, rows.truncated) == ([[1], [2]], True)
    assert charts.run_query(fish.id, "SELECT 1 AS n").truncated is False


def test_a_chart_query_stops_at_the_byte_cap_and_says_so(fish, monkeypatch):
    monkeypatch.setattr(get_settings(), "chart_max_bytes", 100)
    rows = charts.run_query(fish.id, "SELECT repeat('x', 30) FROM generate_series(1, 10)")
    assert rows.truncated is True
    assert 1 <= len(rows.rows) < 10


def test_a_chart_query_is_stopped_at_its_time_cap(fish, monkeypatch):
    monkeypatch.setattr(get_settings(), "chart_statement_timeout", 200)
    with pytest.raises(charts.ChartQueryError, match="statement timeout"):
        charts.run_query(fish.id, "SELECT pg_sleep(5)")


def test_a_chart_query_is_one_statement(fish):
    with pytest.raises(charts.ChartQueryError, match="multiple commands"):
        charts.run_query(fish.id, "SELECT 1; SELECT 2")


# --- Specs -----------------------------------------------------------------------------------

BAR = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "name", "type": "nominal"},
        "y": {"field": "weight_kg", "type": "quantitative"},
    },
}


def with_(**parts) -> dict:
    return {**BAR, **parts}


def test_a_spec_of_allowed_marks_and_transforms_passes():
    assert charts.check_spec(BAR) is None
    layered = {
        "transform": [
            {"aggregate": [{"op": "sum", "field": "w", "as": "total"}], "groupby": ["name"]},
            {"window": [{"op": "rank", "as": "rank"}], "sort": [{"field": "total"}]},
            {"fold": ["a", "b"]},
            {"timeUnit": "yearmonth", "field": "day", "as": "month"},
            {"bin": True, "field": "w", "as": "binned"},
            {"stack": "total", "groupby": ["name"], "as": ["s0", "s1"]},
        ],
        "layer": [
            {"mark": {"type": "line", "point": True}, "encoding": BAR["encoding"]},
            {"mark": "text", "encoding": {**BAR["encoding"], "text": {"field": "name"}}},
        ],
    }
    assert charts.check_spec(layered) is None


@pytest.mark.parametrize(
    ("spec", "refusal"),
    [
        # External data.
        (with_(data={"url": "https://example.com/x.csv"}), "data"),
        ({"layer": [{**BAR, "data": {"url": "data.json"}}]}, "data"),
        (with_(datasets={"x": [{"a": 1}]}), "data"),
        # Links and URL encodings, and images.
        (with_(encoding={**BAR["encoding"], "href": {"field": "name"}}), "link"),
        (with_(mark={"type": "bar", "href": "https://example.com"}), "link"),
        (with_(encoding={**BAR["encoding"], "url": {"field": "name"}}), "URL"),
        (with_(mark="image"), "image"),
        # Signal parameters.
        (with_(params=[{"name": "p", "value": 1}]), "parameter"),
        (with_(params=[{"name": "s", "select": "interval"}]), "parameter"),
        # Expression strings, in every place one goes.
        (with_(transform=[{"calculate": "datum.a * 2", "as": "b"}]), "expression"),
        (with_(transform=[{"filter": "datum.a > 2"}]), "expression"),
        (with_(mark={"type": "bar", "color": {"expr": "'red'"}}), "expression"),
        (
            with_(encoding={**BAR["encoding"], "x": {"field": "name", "axis": {"labelExpr": "1"}}}),
            "expression",
        ),
        (
            with_(
                encoding={
                    **BAR["encoding"],
                    "color": {"condition": {"test": "datum.a > 1", "value": "red"}, "value": "b"},
                }
            ),
            "expression",
        ),
        # Marks and transforms off the allowlist.
        (with_(mark="geoshape"), "mark"),
        (with_(mark="trail"), "mark"),
        (
            with_(transform=[{"lookup": "a", "from": {"data": {"values": []}, "key": "a"}}]),
            "lookup",
        ),
        (with_(transform=[{"regression": "y", "on": "x"}]), "regression"),
        # Not Vega-Lite at all.
        ({"mark": "bar", "encoding": {"x": {"field": "a", "type": "sideways"}}}, "Vega-Lite"),
        ({"marks": []}, "Vega-Lite"),
    ],
)
def test_every_refused_spec_feature_is_rejected(spec, refusal):
    problem = charts.check_spec(spec)
    assert problem is not None
    assert refusal.lower() in problem.lower(), problem


# --- Asking ----------------------------------------------------------------------------------

FISH_SQL = "SELECT name, weight_kg FROM data.fish ORDER BY name"


def answers(*replies: dict, calls: list | None = None):
    """An insights agent answering each question with the next of `replies` (a chart's title,
    sql and spec), each call costing £0.30; the messages each call is sent go in `calls`."""
    queue = list(replies)

    async def script(messages, info):
        if calls is not None:
            calls.append(messages)
        yield Usage(100_000, 10_000)
        yield ToolCall("ChartAnswer", queue.pop(0))

    return model(script)


def chart(sql: str = FISH_SQL, spec: dict | None = None, title: str = "Fish by weight") -> dict:
    return {"title": title, "sql": sql, "spec": spec or BAR}


def ask(api, question: str = "How heavy are the fish?"):
    return api.post("/api/insights/ask", json={"question": question})


def test_asking_draws_the_agents_chart_from_its_query(api, fish):
    calls: list = []
    with use_model(answers(chart(), calls=calls)):
        resp = ask(api)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["title"], body["sql"], body["spec"]) == ("Fish by weight", FISH_SQL, BAR)
    assert body["question"] == "How heavy are the fish?"
    assert body["data"] == [
        {"name": "carp", "weight_kg": "7.5"},
        {"name": "perch", "weight_kg": "0.8"},
        {"name": "pike", "weight_kg": "4.2"},
    ]
    assert (body["truncated"], body["error"]) == (False, None)
    # The agent is told the question, and what it may read: the views and data tables.
    (sent,) = calls
    prompt = "\n".join(str(m.content) for m in sent)
    assert "How heavy are the fish?" in prompt
    assert "data.fish" in prompt and "insights.turns" in prompt
    assert "aios.chat_messages" not in prompt


def test_a_truncated_result_is_drawn_and_marked(api, fish, monkeypatch):
    monkeypatch.setattr(get_settings(), "chart_max_rows", 2)
    with use_model(answers(chart())):
        body = ask(api).json()
    assert (len(body["data"]), body["truncated"]) == (2, True)


def test_a_failing_spec_goes_back_to_the_agent_once_to_fix(api, fish):
    calls: list = []
    bad = {**BAR, "params": [{"name": "p", "value": 1}]}
    with use_model(answers(chart(spec=bad), chart(), calls=calls)):
        body = ask(api).json()

    assert len(calls) == 2
    assert "Signal parameters" in str(calls[1][-1].content)
    assert (body["error"], body["spec"]) == (None, BAR)
    assert len(body["data"]) == 3


def test_a_spec_failing_twice_shows_the_error_with_the_sql(api, fish):
    calls: list = []
    bad = {**BAR, "mark": "geoshape"}
    with use_model(answers(chart(spec=bad), chart(spec=bad), calls=calls)):
        body = ask(api).json()

    assert len(calls) == 2
    assert "geoshape" in body["error"]
    assert body["sql"] == FISH_SQL
    assert body["data"] is None


def test_a_query_failing_twice_shows_the_database_error_with_the_sql(api, fish):
    sql = "SELECT body FROM aios.chat_messages"
    calls: list = []
    with use_model(answers(chart(sql=sql), chart(sql=sql), calls=calls)):
        body = ask(api).json()

    assert len(calls) == 2
    assert "permission denied" in str(calls[1][-1].content)
    assert "permission denied" in body["error"]
    assert (body["sql"], body["data"]) == (sql, None)


def test_each_question_is_an_invocation_charged_to_whoever_asked(api, db, user, fish):
    member = join_organisation(db, user, "asker")
    with use_model(answers(chart(), chart())), signed_in_as(member):
        assert ask(api).status_code == 200

    store = rows_of(user)
    (invocation,) = store.scalars(select(Invocation).where(Invocation.cause == "insight"))
    agent = store.get(Agent, invocation.agent_id)
    assert agent.builtin == "insights"
    assert (invocation.requested_by, invocation.charged_to) == (member.id, member.id)
    assert invocation.status == "succeeded"
    (entry,) = store.scalars(select(UsageEntry).where(UsageEntry.invocation_id == invocation.id))
    assert (entry.purpose, entry.cause, entry.person_id) == ("insight", "insight", member.id)
    assert entry.agent_id == agent.id
    assert entry.cost == pytest.approx(0.30)


def test_asking_past_the_per_person_rate_limit_answers_429(api, db, user, fish, monkeypatch):
    monkeypatch.setattr(get_settings(), "chart_questions_per_hour", 2)
    member = join_organisation(db, user, "other")
    with use_model(answers(*[chart()] * 5)):
        assert ask(api).status_code == 200
        assert ask(api).status_code == 200
        refused = ask(api)
        assert refused.status_code == 429
        assert int(refused.headers["Retry-After"]) > 0
        # Per person: someone else in the organisation still may.
        with signed_in_as(member):
            assert ask(api).status_code == 200


def test_at_the_budget_or_allowance_asking_is_refused_before_any_model_call(api, db, user, fish):
    calls: list = []
    set_budget(api, 0.5)
    with use_model(answers(*[chart()] * 4, calls=calls)):
        assert ask(api).status_code == 200
        assert ask(api).status_code == 200
        refused = ask(api)
    assert refused.status_code == 409
    assert refused.json()["detail"] == budgets.BUDGET_REACHED.format(amount="0.50")
    assert len(calls) == 2

    participant = join_organisation(db, user, "pat", first_name="Pat", last_name="P")
    set_role(db, participant, PARTICIPANT, of=user)
    set_budget(api, 100, participant=0.25)
    with use_model(answers(*[chart()] * 4)), signed_in_as(participant):
        assert ask(api).status_code == 200
        refused = ask(api)
    assert refused.status_code == 409
    assert refused.json()["detail"] == budgets.ALLOWANCE_REACHED.format(name="Pat P", amount="0.25")


@pytest.mark.parametrize("question", ["", "  ", "x" * (charts.MAX_QUESTION + 1)])
def test_a_question_must_be_words_and_not_too_long(api, fish, question):
    assert ask(api, question).status_code == 422


# --- Saved charts ----------------------------------------------------------------------------


def save(api, **values) -> dict:
    resp = api.post(
        "/api/insights/charts", json={**chart(), "question": "How heavy are the fish?", **values}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def no_model():
    async def script(messages, info):
        raise AssertionError("a saved chart never calls the agent")
        yield ""

    return model(script)


def audit_actions(user) -> list[tuple[str, str | None]]:
    rows = rows_of(user).scalars(select(AuditEntry).order_by(AuditEntry.id))
    return [(e.action, e.target_id) for e in rows if e.action.startswith("insight_chart.")]


def test_a_saved_chart_reruns_its_sql_on_current_data_not_the_agent(api, user, fish):
    saved = save(api)
    (listed,) = api.get("/api/insights/charts").json()
    assert (listed["id"], listed["title"], listed["sql"]) == (
        saved["id"],
        "Fish by weight",
        FISH_SQL,
    )
    assert listed["created_by"]["id"] == user.id
    assert listed["can_change"] is True

    with use_model(no_model()):
        data = api.get(f"/api/insights/charts/{saved['id']}/data").json()
        assert [r["name"] for r in data["data"]] == ["carp", "perch", "pike"]
        with as_owner(fish) as conn:
            conn.execute("INSERT INTO data.fish VALUES ('eel', 1.1)")
        insights.clear_cache()
        data = api.get(f"/api/insights/charts/{saved['id']}/data").json()
    assert [r["name"] for r in data["data"]] == ["carp", "eel", "perch", "pike"]
    assert ("insight_chart.saved", str(saved["id"])) in audit_actions(user)


def test_a_saved_chart_runs_under_the_same_role_and_caps(api, fish, monkeypatch):
    saved = save(api)
    monkeypatch.setattr(get_settings(), "chart_max_rows", 1)
    data = api.get(f"/api/insights/charts/{saved['id']}/data").json()
    assert (len(data["data"]), data["truncated"]) == (1, True)
    calls = []
    real = charts.run_query

    def watched(store_id, sql):
        calls.append(sql)
        return real(store_id, sql)

    monkeypatch.setattr(charts, "run_query", watched)
    insights.clear_cache()
    api.get(f"/api/insights/charts/{saved['id']}/data")
    assert calls == [FISH_SQL]


def test_saving_checks_the_spec_and_sql_again(api, fish):
    bad = {**BAR, "mark": "image"}
    resp = api.post("/api/insights/charts", json={**chart(spec=bad), "question": "q"})
    assert resp.status_code == 422
    assert "Images" in resp.json()["detail"]
    resp = api.post(
        "/api/insights/charts", json={**chart(sql="SELECT * FROM aios.memories"), "question": "q"}
    )
    assert resp.status_code == 422
    assert "permission denied" in resp.json()["detail"]
    assert api.get("/api/insights/charts").json() == []


def test_a_chart_whose_query_no_longer_runs_says_so(api, fish):
    saved = save(api)
    with as_owner(fish) as conn:
        conn.execute("DROP TABLE data.fish")
    resp = api.get(f"/api/insights/charts/{saved['id']}/data")
    assert resp.status_code == 409
    assert "no longer works" in resp.json()["detail"]
    assert "data.fish" in resp.json()["detail"]


def test_only_its_creator_or_an_admin_renames_or_deletes_a_chart(api, db, user, fish):
    maker = join_organisation(db, user, "maker")
    set_role(db, maker, PARTICIPANT, of=user)
    other = join_organisation(db, user, "other")
    set_role(db, other, PARTICIPANT, of=user)
    with signed_in_as(maker):
        mine = save(api, title="Their chart")
    with signed_in_as(other):
        (listed,) = api.get("/api/insights/charts").json()
        assert listed["can_change"] is False
        assert (
            api.patch(f"/api/insights/charts/{mine['id']}", json={"title": "Mine"}).status_code
            == 403
        )
        assert api.delete(f"/api/insights/charts/{mine['id']}").status_code == 403
    with signed_in_as(maker):
        resp = api.patch(f"/api/insights/charts/{mine['id']}", json={"title": "Renamed"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "Renamed"
    # The admin may too.
    assert api.get("/api/insights/charts").json()[0]["can_change"] is True
    assert api.delete(f"/api/insights/charts/{mine['id']}").status_code == 204
    assert api.get("/api/insights/charts").json() == []
    assert api.get(f"/api/insights/charts/{mine['id']}/data").status_code == 404
    assert audit_actions(user) == [
        ("insight_chart.saved", str(mine["id"])),
        ("insight_chart.renamed", str(mine["id"])),
        ("insight_chart.deleted", str(mine["id"])),
    ]


def test_a_store_made_before_the_insights_role_gets_it_when_migrated(db, fish):
    import psycopg
    from psycopg import sql as pgsql

    from aios.datastores import provision
    from aios.models import DataStore

    names = provision.StoreNames.of(fish)
    dispose_engines()
    # As it was: no insights role, and no password for one.
    with psycopg.connect(
        libpq_conninfo(get_settings().data_store_provisioner_url), autocommit=True
    ) as conn:
        conn.execute(
            pgsql.SQL("GRANT {} TO CURRENT_USER WITH INHERIT TRUE, SET FALSE").format(
                pgsql.Identifier(names.insights)
            )
        )
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(libpq_conninfo(url, database=fish.dbname), autocommit=True) as conn:
        conn.execute(pgsql.SQL("DROP OWNED BY {}").format(pgsql.Identifier(names.insights)))
    with psycopg.connect(
        libpq_conninfo(get_settings().data_store_provisioner_url), autocommit=True
    ) as conn:
        conn.execute(pgsql.SQL("DROP ROLE {}").format(pgsql.Identifier(names.insights)))
    db.get(DataStore, fish.id).insights_password = None
    db.commit()

    provision.migrate_store(fish.id)

    assert len(charts.run_query(fish.id, "SELECT * FROM data.fish").rows) == 3
    with pytest.raises(charts.ChartQueryError, match="permission denied"):
        charts.run_query(fish.id, "SELECT * FROM aios.chat_messages")
    dispose_engines()
