"""Searchable run history: embedding the exported messages (a second setting of the run history
export, on the Settings page) and searching past runs with hybrid retrieval
(`aios.datastores.history`, `aios.search`)."""

import pytest
from test_history_export import settings, switch

from aios.datastores import data_store_for


def embed(api, on: bool):
    return api.put("/api/settings", json={"embed_run_history": on})


@pytest.fixture
def store_id(user):
    return data_store_for(user.id)


def test_embedding_is_off_by_default_and_offered_only_while_the_export_is_on(api):
    assert settings(api)["embed_run_history"] is False
    assert settings(api)["history_embedded_at"] is None

    # Not while the export is off.
    response = embed(api, True)
    assert response.status_code == 409
    assert settings(api)["embed_run_history"] is False

    switch(api, True)
    response = embed(api, True)
    assert response.status_code == 200
    assert response.json()["embed_run_history"] is True
    assert response.json()["export_run_history"] is True
    assert settings(api)["embed_run_history"] is True

    # Switching the export off switches embedding off with it.
    assert switch(api, False)["embed_run_history"] is False
    switch(api, True)
    assert settings(api)["embed_run_history"] is False
