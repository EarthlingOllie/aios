"""Agents' own database logins (`aios.datastores.agent_roles`), and the escape suite.

Each agent that may write has its own login in its store, granted write on exactly the `data`
tables it owns or is shared (and those nobody owns, through its group), and read on the rest of
the store; `write_data` writes as it. Model-written SQL (`query_data`) stays on the store's
read-only role.

The escape suite (`test_escape_*`) is adversarial, against a real store: acting as an agent's
login, and as model-written SQL through `query_data`, every known way out of the role is refused
or has no effect. It's a required check: it runs in the backend shards with the rest."""

import base64
import hashlib
import hmac
import os

import psycopg
import pytest
from conftest import as_agent, role_row, store_of
from test_data_tools import as_owner, provisioned, run_tools
from test_table_ownership import create_table, drop_table, share, write, writer

from aios.config import get_settings, libpq_conninfo
from aios.datastores import agent_roles, connections, credentials, provision
from aios.db import SessionLocal
from aios.models import DataStore
from aios.testing.fake_model import ToolCall
from aios.worker import URGENT, celery_app

# --- Helpers ------------------------------------------------------------------------------------


@pytest.fixture
def store(db, user) -> DataStore:
    """The signed-in admin's store, provisioned, with an unowned `data.fish` table."""
    row = provisioned(db, store_of(user))
    with as_owner(row) as conn:
        conn.execute("CREATE TABLE data.fish (id int PRIMARY KEY, name text)")
        conn.execute("INSERT INTO data.fish VALUES (1, 'pike')")
    return row


def query(api, agent: dict, sql: str) -> dict:
    [result] = run_tools(api, agent, ToolCall("query_data", {"sql": sql}))
    return result


def rows(store: DataStore, sql: str) -> list[tuple]:
    with as_owner(store) as conn:
        return conn.execute(sql).fetchall()


def server(sql: str, *params) -> list[tuple]:
    """As the provisioner, on the platform database."""
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(libpq_conninfo(url), autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def admin(sql: str, *params) -> list[tuple]:
    """As the master user (what can read `pg_authid`); skips without one."""
    url = os.environ.get("DATABASE_ADMIN_URL")
    if not url:
        pytest.skip("DATABASE_ADMIN_URL isn't set")
    with psycopg.connect(libpq_conninfo(url), autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def verifies(role: str, password: str) -> bool:
    """Whether the role's SCRAM verifier in `pg_authid` is one of `password`."""
    [(verifier,)] = admin("SELECT rolpassword FROM pg_authid WHERE rolname = %s", role)
    method, _, rest = verifier.partition("$")
    params, _, keys = rest.partition("$")
    iterations, salt = params.split(":")
    stored_key, _ = keys.split(":")
    salted = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), base64.b64decode(salt), int(iterations)
    )
    client_key = hmac.digest(salted, b"Client Key", "sha256")
    return method == "SCRAM-SHA-256" and hashlib.sha256(client_key).digest() == base64.b64decode(
        stored_key
    )


@pytest.fixture
def scribe(api, store) -> dict:
    """An agent with the write tool, owning `data.notes`."""
    agent = writer(api, "Scribe")
    create_table(api, store, agent, "notes")
    return agent


@pytest.fixture
def other(api, store) -> dict:
    """Another agent with the write tool, owning `data.letters`."""
    agent = writer(api, "Other")
    create_table(api, store, agent, "letters")
    return agent


# --- The logins ---------------------------------------------------------------------------------


def test_each_writing_agent_gets_its_own_login_with_a_password_postgres_holds_as_a_verifier(
    api, store, scribe
):
    row = role_row(store, scribe)

    assert row.role_name == f"{store.role_prefix}_agent_{scribe['id']}"
    password = credentials.decrypt(row.password)
    assert password not in row.password  # kept encrypted
    [(verifier,)] = admin("SELECT rolpassword FROM pg_authid WHERE rolname = %s", row.role_name)
    assert verifier.startswith("SCRAM-SHA-256$") and password not in verifier
    with as_agent(store, scribe) as conn:
        assert conn.execute("SELECT current_user, session_user").fetchone() == (
            row.role_name,
            row.role_name,
        )


def test_the_login_inherits_its_group_but_cant_switch_to_it_and_has_its_own_limit(
    api, store, scribe
):
    row = role_row(store, scribe)
    group = f"{store.role_prefix}_agents"

    assert server(
        "SELECT r.rolname, m.inherit_option, m.set_option, m.admin_option"
        " FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid"
        " WHERE m.member = to_regrole(%s)",
        row.role_name,
    ) == [(group, True, False, False)]
    [(can_login, inherit, limit, privileged)] = server(
        "SELECT rolcanlogin, rolinherit, rolconnlimit, rolsuper OR rolcreaterole OR rolcreatedb"
        " OR rolbypassrls OR rolreplication FROM pg_roles WHERE rolname = %s",
        row.role_name,
    )
    assert (can_login, inherit, privileged) == (True, False, False)
    assert limit == get_settings().data_store_agent_connection_limit
    [(group_logs_in,)] = server("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", group)
    assert group_logs_in is False


def test_the_store_migration_command_makes_and_verifies_every_writing_agents_login(
    api, store, agent_access
):
    agent_access.auto = False
    late = writer(api, "Late")
    assert role_row(store, late) is None

    outcome = provision.migrate_all([store.id])

    assert not isinstance(outcome[store.id], BaseException), outcome
    assert role_row(store, late) is not None
    assert agent_roles.verify(store.id) == []


def test_verifying_finds_grants_that_drifted_from_the_records(api, store, scribe, other):
    with as_owner(store) as conn:
        conn.execute(f'GRANT INSERT ON data.letters TO "{store.role_prefix}_agent_{scribe["id"]}"')

    problems = agent_roles.verify(store.id)

    assert problems and "data.letters" in problems[0]
    agent_roles.apply(store.id)
    assert agent_roles.verify(store.id) == []


def test_write_data_writes_as_the_agents_own_login(api, store, scribe):
    with as_owner(store) as conn:
        conn.execute("ALTER TABLE data.notes ADD COLUMN who text DEFAULT current_user")

    assert write(api, scribe, "notes", note="mine")["row_count"] == 1

    assert rows(store, "SELECT who FROM data.notes") == [(role_row(store, scribe).role_name,)]


def test_an_agent_login_reads_conversation_and_memory_but_never_the_deny_list(store, scribe):
    with as_agent(store, scribe) as conn:
        for table in ("aios.chat_messages", "aios.chats", "aios.turns", "aios.memories"):
            conn.execute(f"SELECT * FROM {table} LIMIT 1")
        conn.execute("SELECT * FROM langgraph.checkpoints LIMIT 1")
        for denied in (
            "SELECT email FROM aios.people",
            "SELECT secret FROM aios.sandbox_sessions",
            "SELECT details FROM aios.audit_log",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(denied)


# --- Applying: pending, being set up, the urgent lane ------------------------------------------


def test_applying_runs_on_the_urgent_lane():
    assert celery_app.conf.task_routes["aios.apply_agent_access"] == {"queue": URGENT}


def test_a_share_is_pending_until_applied_and_a_write_meanwhile_is_told_to_try_again(
    api, store, scribe, agent_access
):
    helper = writer(api, "Helper")
    agent_access.auto = False

    assert share(api, "notes", helper).json()["pending"] is True
    tables = {t["name"]: t for t in api.get("/api/data/tables").json()["tables"]}
    assert tables["notes"]["pending"] is True
    assert agent_access.queued == [store.id]

    early = write(api, helper, "notes", note="early")
    assert "being set up" in early["error"] and "try again" in early["error"]
    assert rows(store, "SELECT note FROM data.notes") == []

    agent_access.apply()

    tables = {t["name"]: t for t in api.get("/api/data/tables").json()["tables"]}
    assert tables["notes"]["pending"] is False
    assert write(api, helper, "notes", note="now")["row_count"] == 1


def test_a_new_writing_agents_first_write_is_told_to_try_again_without_failing_the_turn(
    api, store, agent_access
):
    agent_access.auto = False
    fresh = writer(api, "Fresh")

    first, second = run_tools(
        api,
        fresh,
        ToolCall("write_data", {"table": "fish", "operation": "insert", "rows": [{"id": 2}]}),
        ToolCall("query_data", {"sql": "SELECT count(*) FROM data.fish"}),
    )

    assert "being set up" in first["error"]
    assert second["rows"] == [[1]]  # the turn went on
    agent_access.apply()
    assert write(api, fresh, "fish", id=2, name="eel")["row_count"] == 1


# --- Archiving ----------------------------------------------------------------------------------


def test_archiving_closes_connections_and_revokes_grants_and_unarchiving_restores_the_same_login(
    api, store, scribe
):
    before = role_row(store, scribe)
    held = as_agent(store, scribe)

    assert api.post(f"/api/agents/{scribe['id']}/archive").status_code == 200

    with pytest.raises(psycopg.OperationalError):
        held.execute("SELECT 1")
    with pytest.raises(psycopg.OperationalError):
        as_agent(store, scribe)
    role = before.role_name
    assert server("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", role) == [(False,)]
    with as_owner(store) as conn:
        (writes,) = conn.execute(
            "SELECT has_table_privilege(%s, 'data.notes', 'INSERT')"
            " OR has_table_privilege(%s, 'data.fish', 'INSERT')",
            (role, role),
        ).fetchone()
    assert writes is False

    assert api.post(f"/api/agents/{scribe['id']}/unarchive").status_code == 200

    after = role_row(store, scribe)
    assert (after.id, after.role_name, after.password) == (before.id, role, before.password)
    assert write(api, scribe, "notes", note="back")["row_count"] == 1


# --- The pooler ---------------------------------------------------------------------------------


def test_agent_logins_connect_through_the_pooler_with_their_own_small_pool(
    api, store, scribe, monkeypatch
):
    with SessionLocal() as db:
        row = db.get(DataStore, store.id)
    monkeypatch.setattr(get_settings(), "data_store_pooler_address", "pooler.internal:6432")
    agent_login = role_row(store, scribe)
    url = connections.store_url(row, "rw", (agent_login.role_name, agent_login.password))
    assert (url.host, url.port, url.database) == ("pooler.internal", 6432, store.dbname)
    assert url.username == role_row(store, scribe).role_name

    monkeypatch.setattr(get_settings(), "data_store_pooler_address", "")
    with connections.agent_session(store.id, scribe["id"]) as session:
        engine = session.get_bind()
        assert (engine.pool.size(), engine.pool._max_overflow) == (1, 1)


# --- The escape suite ---------------------------------------------------------------------------


@pytest.fixture
def escape(api, store, scribe, other) -> dict:
    """Two agents, each owning a table; an archived owner's table; a dropped table."""
    gone = writer(api, "Gone")
    create_table(api, store, gone, "relics")
    assert api.post(f"/api/agents/{gone['id']}/archive").status_code == 200
    create_table(api, store, scribe, "scraps")
    dropped = drop_table(store, "scraps")
    names = provision.StoreNames.of(store)
    return {
        "names": names,
        "other_role": names.agent(other["id"]),
        "dropped": dropped,
    }


def refused(conn: psycopg.Connection, statement: str) -> None:
    """The statement fails (and, in autocommit, changes nothing)."""
    with pytest.raises(psycopg.Error):
        conn.execute(statement)


def unchanged(store: DataStore) -> None:
    assert rows(store, "SELECT count(*) FROM data.letters") == [(0,)]
    assert rows(store, "SELECT count(*) FROM data.fish") == [(1,)]


def test_escape_resetting_the_role_and_writing_another_agents_table(store, scribe, escape):
    with as_agent(store, scribe) as conn:
        conn.execute("RESET ROLE")
        refused(conn, "INSERT INTO data.letters (note) VALUES ('x')")
        conn.execute("SET ROLE NONE")
        refused(conn, "INSERT INTO data.letters (note) VALUES ('x')")
    unchanged(store)


@pytest.mark.parametrize("target", ["rw", "owner", "group", "other", "ro"])
def test_escape_switching_role(store, scribe, escape, target):
    names = escape["names"]
    role = {
        "rw": names.rw,
        "ro": names.ro,
        "owner": names.owner,
        "group": names.group,
        "other": escape["other_role"],
    }[target]
    with as_agent(store, scribe) as conn:
        refused(conn, f'SET ROLE "{role}"')
        refused(conn, f"SELECT set_config('role', '{role}', false)")
        refused(conn, f'SET SESSION AUTHORIZATION "{role}"')
        refused(conn, f"SELECT set_config('session_authorization', '{role}', false)")


@pytest.mark.parametrize("target", ["rw", "owner", "group", "agent"])
def test_escape_switching_role_through_query_data(api, store, scribe, escape, target):
    names = escape["names"]
    role = {
        "rw": names.rw,
        "owner": names.owner,
        "group": names.group,
        "agent": names.agent(scribe["id"]),
    }[target]
    for sql in (
        f"SELECT set_config('role', '{role}', false)",
        f"SELECT set_config('session_authorization', '{role}', false)",
    ):
        assert "error" in query(api, scribe, sql), sql
    # And a statement that isn't a query isn't run at all.
    assert "error" in query(api, scribe, f'SET ROLE "{role}"')


def test_escape_making_the_read_only_transaction_read_write(api, store, scribe, escape):
    for sql in (
        "SELECT set_config('transaction_read_only', 'off', true), (SELECT count(*) FROM data.fish)",
        "WITH gone AS (DELETE FROM data.fish RETURNING *) SELECT * FROM gone",
        "WITH x AS (INSERT INTO data.letters (note) VALUES ('x') RETURNING *) SELECT * FROM x",
        "WITH x AS (UPDATE data.notes SET note = 'x' RETURNING *) SELECT * FROM x",
    ):
        result = query(api, scribe, sql)
        assert "error" in result or result.get("rows") == [["off", 1]], sql
    unchanged(store)
    with as_agent(store, scribe) as conn:
        conn.execute("BEGIN READ ONLY")
        conn.execute("SELECT 1")
        refused(conn, "SET TRANSACTION READ WRITE")
        conn.execute("ROLLBACK")
        conn.execute("BEGIN READ ONLY")
        conn.execute("SET TRANSACTION READ WRITE")  # before any query: allowed, and...
        refused(
            conn,
            "WITH x AS (INSERT INTO data.letters (note) VALUES ('x') RETURNING *) SELECT * FROM x",
        )  # ... still not its table
        conn.execute("ROLLBACK")
    unchanged(store)


def test_escape_altering_ones_own_role(store, scribe, escape):
    role = escape["names"].agent(scribe["id"])
    with as_agent(store, scribe) as conn:
        refused(conn, f'ALTER ROLE "{role}" CONNECTION LIMIT 1000')
        refused(conn, f'ALTER ROLE "{role}" SUPERUSER')
        refused(conn, f'ALTER ROLE "{role}" CREATEROLE')
        refused(conn, f"ALTER ROLE \"{escape['names'].rw}\" PASSWORD 'stolen'")
        refused(conn, f"ALTER ROLE \"{escape['other_role']}\" PASSWORD 'stolen'")
        # Postgres lets any role change its own password and its own settings: neutralised, as
        # applying access sets the platform's verifier and settings again (and a write that
        # can't log in queues that).
        conn.execute(f"ALTER ROLE \"{role}\" PASSWORD 'stolen'")
        conn.execute(f'ALTER ROLE "{role}" SET statement_timeout = 0')
        conn.execute(f'ALTER ROLE "{role}" IN DATABASE "{store.dbname}" SET work_mem = \'1GB\'')
    agent_roles.apply(store.id)
    with as_agent(store, scribe) as conn:
        assert conn.execute("SELECT current_user").fetchone() == (role,)
        (timeout,) = conn.execute("SHOW statement_timeout").fetchone()
        assert timeout != "0"
        assert conn.execute("SHOW work_mem").fetchone() != ("1GB",)
    # What Postgres checks a password against is the platform's again, not the stolen one (by
    # the verifier: the test server may trust local connections whatever their password).
    platform = credentials.decrypt(role_row(store, scribe).password)
    assert verifies(role, platform) and not verifies(role, "stolen")


def test_escape_security_definer_functions(api, store, scribe, escape):
    assert (
        rows(
            store,
            "SELECT p.oid::regprocedure::text FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog', 'information_schema')",
        )
        == []
    )
    with as_agent(store, scribe) as conn:
        for schema in ("data", "aios", "extensions", "langgraph", "pg_temp"):
            refused(
                conn,
                f"CREATE FUNCTION {schema}.escape() RETURNS int LANGUAGE sql SECURITY DEFINER"
                " AS 'SELECT 1'",
            )
        refused(conn, "DO $$ BEGIN INSERT INTO data.letters (note) VALUES ('x'); END $$")
    assert "error" in query(
        api,
        scribe,
        "WITH f AS (SELECT 1) SELECT * FROM f; CREATE FUNCTION data.escape() RETURNS int"
        " LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
    )
    unchanged(store)


CONVERSATION_WRITES = [
    "INSERT INTO aios.chat_messages (chat_id, body) VALUES (1, 'forged')",
    "UPDATE aios.chat_messages SET body = 'forged'",
    "DELETE FROM aios.chat_messages",
    "UPDATE aios.chats SET name = 'forged'",
    "DELETE FROM aios.turns",
    "UPDATE aios.agent_runs SET agent_id = agent_id",
    "DELETE FROM langgraph.checkpoints",
    "DELETE FROM langgraph.checkpoint_writes",
    "UPDATE aios.memories SET agent_id = agent_id",
    "DELETE FROM aios.memories",
    "UPDATE aios.table_owners SET agent_id = NULL",
    "DELETE FROM aios.table_shares",
    "UPDATE aios.agents SET archived_at = NULL",
    "UPDATE aios.audit_log SET action = 'forged'",
    "DELETE FROM aios.audit_log",
    "UPDATE aios.events SET payload = '{}'",
]


@pytest.mark.parametrize("statement", CONVERSATION_WRITES)
def test_escape_writing_conversation_content_or_memory_as_an_agent(
    store, scribe, escape, statement
):
    with as_agent(store, scribe) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


def test_escape_writing_conversation_content_or_memory_through_query_data(
    api, store, scribe, escape
):
    for statement in CONVERSATION_WRITES:
        sql = f"WITH x AS ({statement} RETURNING 1) SELECT * FROM x"
        assert "error" in query(api, scribe, sql), statement


@pytest.mark.parametrize(
    "statement",
    [
        "COPY (SELECT 1) TO PROGRAM 'id'",
        "COPY data.fish FROM PROGRAM 'id'",
        "COPY data.fish TO '/tmp/escape'",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_read_binary_file('/etc/passwd')",
        "SELECT pg_ls_dir('/')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT lo_export(1, '/tmp/escape')",
        "CREATE EXTENSION dblink",
        "CREATE TEMP TABLE escape (id int)",
    ],
)
def test_escape_server_files_and_programs(api, store, scribe, escape, statement):
    with as_agent(store, scribe) as conn:
        refused(conn, statement)
    assert "error" in query(api, scribe, statement)


@pytest.mark.parametrize(
    "call",
    [
        {"table": 'fish"; DROP TABLE data.fish; --', "operation": "insert", "rows": [{"id": 9}]},
        {"table": "data.fish; DROP TABLE data.fish", "operation": "insert", "rows": [{"id": 9}]},
        {"table": "aios.chat_messages", "operation": "delete", "where": {"id": 1}},
        {"table": "../aios.memories", "operation": "delete", "where": {"id": 1}},
        {
            "table": "fish",
            "operation": "insert",
            "rows": [{"id) VALUES (9); DROP TABLE data.fish; --": 9}],
        },
        {
            "table": "fish",
            "operation": "update",
            "values": {"name = 'x' --": "y"},
            "where": {"id": 1},
        },
        {"table": "fish", "operation": "delete", "where": {"id = id OR 1=1 --": 1}},
        {
            "table": "fish",
            "operation": "upsert",
            "rows": [{"id": 1, "name": "x"}],
            "on_conflict": ["id) DO NOTHING; DROP TABLE data.fish; --"],
        },
    ],
)
def test_escape_injecting_sql_through_the_write_tools_names(api, store, scribe, escape, call):
    [result] = run_tools(api, scribe, ToolCall("write_data", call))

    assert "error" in result
    assert rows(store, "SELECT id, name FROM data.fish") == [(1, "pike")]
    assert rows(store, "SELECT count(*) FROM aios.chat_messages") == [(0,)]


def test_escape_writing_another_agents_an_archived_owners_or_a_dropped_table(store, scribe, escape):
    with as_agent(store, scribe) as conn:
        for statement in (
            "INSERT INTO data.letters (note) VALUES ('x')",
            "UPDATE data.letters SET note = 'x'",
            "DELETE FROM data.letters",
            "TRUNCATE data.letters",
            "INSERT INTO data.relics (note) VALUES ('x')",
            f"INSERT INTO data.\"{escape['dropped']}\" (note) VALUES ('x')",
            "ALTER TABLE data.letters ADD COLUMN x int",
            "DROP TABLE data.letters",
            "CREATE TABLE data.mine (id int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
        # Granting what it hasn't the grant option of grants nothing (Postgres only warns).
        conn.execute("GRANT INSERT ON data.letters, data.notes TO PUBLIC")
        # What it does own, it writes; what nobody owns, too.
        conn.execute("INSERT INTO data.notes (note) VALUES ('mine')")
        conn.execute("INSERT INTO data.fish VALUES (2, 'eel')")
        conn.execute("DELETE FROM data.fish WHERE id = 2")
    assert rows(store, "SELECT count(*) FROM data.letters") == [(0,)]
    assert rows(store, "SELECT count(*) FROM data.relics") == [(0,)]
    assert rows(
        store,
        "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a"
        " WHERE c.relname IN ('letters', 'notes') AND a.grantee = 0",
    ) == [(0,)]


def test_escape_connecting_to_another_store_or_the_platform_database(
    api, db, store, scribe, escape, new_user
):
    elsewhere = provisioned(db, store_of(new_user()))
    platform = psycopg.conninfo.conninfo_to_dict(libpq_conninfo(get_settings().database_url))
    for database in (elsewhere.dbname, platform["dbname"], "postgres", "template1"):
        with pytest.raises(psycopg.OperationalError):
            as_agent(store, scribe, database)
    # Model-written SQL has no way to connect anywhere.
    for sql in (
        f"SELECT * FROM dblink('dbname={elsewhere.dbname}', 'SELECT 1') AS t(x int)",
        "SELECT * FROM pg_catalog.pg_stat_activity WHERE false",
    ):
        result = query(api, scribe, sql)
        assert "error" in result or result["rows"] == [], sql
