"""Proposing and applying schema changes on a real, provisioned store
(`aios.datastores.schema_changes`): a request is checked against the store's schema when it's
made, and applied (as the store's owner, in one transaction) once approved."""

import threading

import psycopg
import pytest
from conftest import StoreRows, store_of
from sqlalchemy import update
from test_data_tools import as_owner, provisioned

from aios.config import get_settings
from aios.datastores import schema_changes
from aios.datastores.models import Approval
from aios.datastores.schema_changes import Invalid, SchemaChange
from aios.models import ApprovalStatus, DataStore


@pytest.fixture
def store(db, user) -> DataStore:
    """The organisation's store, provisioned, with a hand-made `data.fish` table holding a fish."""
    row = provisioned(db, store_of(user))
    with as_owner(row) as conn:
        conn.execute("CREATE TABLE data.fish (id bigint PRIMARY KEY, name text)")
        conn.execute("INSERT INTO data.fish VALUES (1, 'pike')")
    return row


def spec(*ops: dict) -> SchemaChange:
    return SchemaChange.model_validate({"ops": list(ops)})


CONTACTS = {
    "op": "create_table",
    "name": "contacts",
    "description": "People",
    "columns": [
        {"name": "name", "type": "text", "nullable": False},
        {"name": "bio", "type": "text"},
        {"name": "bio_embedding", "type": "embedding", "embedding_of": "bio"},
    ],
}


# The store each request `propose` made is in, by the request's id.
STORE_OF: dict = {}


def propose(store: DataStore, *ops: dict) -> Approval:
    request = schema_changes.propose(
        store.id, spec(*ops), "Because", run_id=None, tool_call_id=None
    )
    STORE_OF[request.id] = store.id
    return request


def approve(db, request: Approval) -> Approval:
    """Approve the request behind the API's back and apply it; returns it as it is then."""
    rows = StoreRows(STORE_OF[request.id])
    rows.execute(
        update(Approval).where(Approval.id == request.id).values(status=ApprovalStatus.approved)
    )
    schema_changes.apply(rows.store_id, request.id)
    return rows.get(Approval, request.id)


def tables(store: DataStore) -> dict[str, list[str]]:
    with as_owner(store) as conn:
        rows = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'data' ORDER BY table_name, ordinal_position"
        )
        out: dict[str, list[str]] = {}
        for table, column in rows:
            out.setdefault(table, []).append(column)
        return out


def history(store: DataStore) -> list[tuple]:
    with as_owner(store) as conn:
        return conn.execute(
            "SELECT id, request_id, summary FROM aios.schema_history ORDER BY id"
        ).fetchall()


def test_a_request_is_checked_against_the_stores_schema_and_compiled(db, store):
    request = propose(store, CONTACTS)

    assert request.status == ApprovalStatus.pending
    assert request.base_revision == 0
    assert request.sql.startswith('CREATE TABLE "data"."contacts" (')
    assert request.summary == "Create table data.contacts"
    assert request.reason == "Because"
    assert request.ops[0]["name"] == "contacts"
    with pytest.raises(Invalid, match="`data.fish` already exists"):
        propose(store, {**CONTACTS, "name": "fish"})


def test_an_approved_request_is_applied_in_one_go_and_logged(db, store):
    request = propose(
        store,
        CONTACTS,
        {"op": "create_index", "table": "contacts", "columns": ["name"]},
        {"op": "add_column", "table": "fish", "column": {"name": "weight_kg", "type": "numeric"}},
    )

    applied = approve(db, request)

    assert applied.status == ApprovalStatus.applied
    assert applied.applied_at is not None and applied.error is None
    assert tables(store) == {
        "contacts": ["id", "name", "bio", "bio_embedding"],
        "fish": ["id", "name", "weight_kg"],
    }
    [(revision, request_id, summary)] = history(store)
    assert request_id == request.id
    assert summary == (
        "Create table data.contacts; create index contacts_name_idx on data.contacts;"
        " add column weight_kg to data.fish"
    )
    with as_owner(store) as conn:
        assert conn.execute("SELECT * FROM aios.embedding_columns").fetchall() == [
            ("contacts", "bio_embedding", "bio")
        ]
        owner = conn.execute(
            "SELECT tableowner FROM pg_tables WHERE schemaname = 'data' AND tablename = 'contacts'"
        ).fetchone()[0]
    assert owner == f"{store.role_prefix}_owner"
    # The revision is bumped: a new request is checked against it.
    assert propose(store, {**CONTACTS, "name": "more"}).base_revision == revision


def test_the_read_write_role_reads_the_new_table_but_never_writes_it(db, store):
    """Only agents' own logins write data tables (`aios.datastores.agent_roles`)."""
    approve(db, propose(store, CONTACTS))

    with psycopg.connect(_login(store, "rw"), autocommit=True) as rw:
        assert rw.execute("SELECT name FROM data.contacts").fetchall() == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rw.execute("INSERT INTO data.contacts (name) VALUES ('Ada')")


def _login(store: DataStore, role: str) -> str:
    from test_provisioning import login_url

    return login_url(store, role)


def test_a_failed_apply_leaves_the_schema_unchanged(db, store):
    """The new table is made, then a unique index on a column with duplicates fails: the
    table goes too, as nothing of the request is kept."""
    with as_owner(store) as conn:
        conn.execute("INSERT INTO data.fish VALUES (2, 'pike')")
    request = propose(
        store,
        CONTACTS,
        {"op": "create_index", "table": "fish", "columns": ["name"], "unique": True},
    )

    failed = approve(db, request)

    assert failed.status == ApprovalStatus.failed
    assert failed.error == 'could not create unique index "fish_name_idx"'
    assert tables(store) == {"fish": ["id", "name"]}
    assert history(store) == []
    with as_owner(store) as conn:
        assert conn.execute("SELECT count(*) FROM aios.embedding_columns").fetchone() == (0,)


def test_two_requests_on_the_same_revision_the_second_is_checked_again(db, store):
    first = propose(store, CONTACTS)
    clashing = propose(store, {**CONTACTS, "columns": [{"name": "email", "type": "text"}]})
    unrelated = propose(
        store, {"op": "add_column", "table": "fish", "column": {"name": "kg", "type": "numeric"}}
    )
    assert first.base_revision == clashing.base_revision == unrelated.base_revision

    assert approve(db, first).status == ApprovalStatus.applied
    stale = approve(db, clashing)
    applied = approve(db, unrelated)

    assert stale.status == ApprovalStatus.stale
    assert "`data.contacts` already exists" in stale.error
    assert applied.status == ApprovalStatus.applied
    assert tables(store) == {
        "contacts": ["id", "name", "bio", "bio_embedding"],
        "fish": [
            "id",
            "name",
            "kg",
        ],
    }
    assert [h[1] for h in history(store)] == [first.id, unrelated.id]


def test_a_change_applied_while_a_request_is_made_still_gets_it_checked_again(
    db, store, monkeypatch
):
    """The request's revision is never newer than the schema it was checked against, so one
    applied in between is caught at apply (stale), not left to fail there."""
    first = propose(store, CONTACTS)
    read_schema = schema_changes.read_schema

    def first_lands_meanwhile(pg):
        schema = read_schema(pg)
        approve(db, first)
        return schema

    monkeypatch.setattr(schema_changes, "read_schema", first_lands_meanwhile)
    second = propose(store, CONTACTS)
    monkeypatch.undo()

    assert approve(db, second).status == ApprovalStatus.stale


def test_an_apply_waits_for_a_lock_no_longer_than_its_timeout(db, store, monkeypatch):
    monkeypatch.setattr(get_settings(), "schema_change_lock_timeout", 200)
    request = propose(
        store, {"op": "add_column", "table": "fish", "column": {"name": "kg", "type": "numeric"}}
    )
    locked, release = threading.Event(), threading.Event()

    def hold_lock():
        with as_owner(store) as conn, conn.transaction():
            conn.execute("LOCK TABLE data.fish IN ACCESS SHARE MODE")
            locked.set()
            release.wait(10)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        locked.wait(5)
        failed = approve(db, request)
    finally:
        release.set()
        holder.join()

    assert failed.status == ApprovalStatus.failed
    assert "lock timeout" in failed.error
    assert tables(store) == {"fish": ["id", "name"]}


def test_only_an_approved_request_is_applied(db, store):
    request = propose(store, CONTACTS)

    schema_changes.apply(store.id, request.id)

    assert StoreRows(store.id).get(Approval, request.id).status == (ApprovalStatus.pending)
    assert tables(store) == {"fish": ["id", "name"]}
