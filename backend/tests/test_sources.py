"""Sources: what brings content into an organisation's store. Every store has exactly one,
Uploads, made when it's provisioned, which every uploaded document belongs to; it can't be
paused or removed. Every member sees the sources; only admins change them."""

import pytest
from conftest import add_document, delete_user, provisioned_user, store_of
from psycopg import errors
from sqlalchemy.exc import IntegrityError
from test_chats import signed_in_as
from test_documents import queued, upload  # noqa: F401  (a fixture)
from test_permissions import participant, set_role  # noqa: F401  (a fixture)

from aios.datastores import store_session
from aios.datastores.models import Document, Source, SourceKind
from aios.models import Role

UPLOADS = {"kind": "upload", "name": "Uploads", "status": "active"}


def sources(api) -> list[dict]:
    resp = api.get("/api/sources")
    assert resp.status_code == 200, resp.text
    return resp.json()


def uploads_of(api) -> dict:
    [uploads] = [s for s in sources(api) if s["kind"] == "upload"]
    return uploads


def add_source(store_id, name: str, kind: SourceKind = SourceKind.url) -> int:
    """A source other than Uploads, as later kinds will add (there's no route to add one yet)."""
    with store_session(store_id, "rw") as store:
        source = Source(kind=kind, name=name, settings={})
        store.add(source)
        store.commit()
        return source.id


def test_a_new_store_has_exactly_one_source_uploads(api):
    listed = sources(api)

    assert [{k: s[k] for k in UPLOADS} for s in listed] == [UPLOADS]
    assert listed[0]["document_count"] == 0
    one = api.get(f"/api/sources/{listed[0]['id']}")
    assert one.status_code == 200
    assert one.json() == listed[0] | {
        "syncs": [],
        "webhook_url": None,
        "webhook_secret": None,
        "email_address": None,
    }


def test_there_is_never_a_second_uploads_source(user):
    with store_session(store_of(user), "rw") as store:
        store.add(Source(kind=SourceKind.upload, name="More uploads", settings={}))
        with pytest.raises(IntegrityError) as raised:
            store.flush()
    assert isinstance(raised.value.orig, errors.UniqueViolation)


def test_every_member_sees_the_sources(api, participant):  # noqa: F811
    with signed_in_as(participant):
        assert [s["name"] for s in sources(api)] == ["Uploads"]


def test_an_upload_belongs_to_uploads(api, queued):  # noqa: F811
    uploads = uploads_of(api)

    doc = upload(api, "a.md", b"# Hi", "text/markdown").json()

    source = {"id": uploads["id"], "kind": "upload", "name": "Uploads"}
    assert doc["source"] == source
    [listed] = api.get("/api/documents").json()
    assert listed["source"] == source
    assert uploads_of(api)["document_count"] == 1


def test_a_document_cant_be_written_without_a_source(user):
    with store_session(store_of(user), "rw") as store:
        store.add(Document(filename="x.md", s3_key="", size_bytes=0, sha256=""))
        with pytest.raises(IntegrityError) as raised:
            store.flush()
    assert isinstance(raised.value.orig, errors.NotNullViolation)


def test_documents_are_filtered_by_source(api, user):
    store_id = store_of(user)
    uploads = uploads_of(api)
    other = add_source(store_id, "Price page")
    uploaded = add_document(store_id, filename="mine.md")
    fetched = add_document(store_id, filename="prices.md", source_id=other)

    every = [d["id"] for d in api.get("/api/documents").json()]
    only_uploads = api.get("/api/documents", params={"source_id": uploads["id"]}).json()
    only_other = api.get("/api/documents", params={"source_id": other}).json()

    assert sorted(every) == sorted([uploaded, fetched])
    assert [d["id"] for d in only_uploads] == [uploaded]
    assert [(d["id"], d["source"]["name"]) for d in only_other] == [(fetched, "Price page")]
    assert api.get("/api/documents", params={"source_id": 987654}).json() == []


@pytest.mark.parametrize("change", [{"paused": True}, {"name": "My files"}])
def test_uploads_cant_be_paused_or_changed(api, change):
    uploads = uploads_of(api)

    resp = api.patch(f"/api/sources/{uploads['id']}", json=change)

    assert resp.status_code == 409
    assert "Uploads can't be paused or removed" in resp.json()["detail"]
    assert uploads_of(api) == uploads


def test_uploads_cant_be_removed(api):
    uploads = uploads_of(api)

    resp = api.delete(f"/api/sources/{uploads['id']}")

    assert resp.status_code == 409
    assert "Uploads can't be paused or removed" in resp.json()["detail"]
    assert uploads_of(api) == uploads


def test_an_admin_pauses_and_resumes_another_source(api, user):
    other = add_source(store_of(user), "Price page")

    paused = api.patch(f"/api/sources/{other}", json={"paused": True})
    assert (paused.status_code, paused.json()["status"]) == (200, "paused")
    resumed = api.patch(f"/api/sources/{other}", json={"paused": False, "name": "Prices"})
    assert (resumed.json()["status"], resumed.json()["name"]) == ("active", "Prices")


def test_only_admins_change_or_remove_sources(api, db, user, participant):  # noqa: F811
    uploads = uploads_of(api)
    for role in (Role.participant, Role.editor):
        set_role(db, participant, role, of=user)
        with signed_in_as(participant):
            for resp in (
                api.patch(f"/api/sources/{uploads['id']}", json={"paused": True}),
                api.delete(f"/api/sources/{uploads['id']}"),
            ):
                assert resp.status_code == 403
                assert "sources" in resp.json()["detail"]


def test_another_organisations_source_is_a_404(api, db):
    other = provisioned_user(db, "other-org")
    try:
        theirs = add_source(store_of(other), "Theirs")
        assert theirs not in {s["id"] for s in sources(api)}

        assert api.get(f"/api/sources/{theirs}").status_code == 404
        assert api.patch(f"/api/sources/{theirs}", json={"paused": True}).status_code == 404
        assert api.delete(f"/api/sources/{theirs}").status_code == 404
    finally:
        delete_user(db, other)
