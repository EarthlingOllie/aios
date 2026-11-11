from langfuse.langchain import CallbackHandler
from pydantic import SecretStr
from test_agents import chat_body, make_agent, text_deltas

from aios import tracing
from aios.config import get_settings
from aios.testing.fake_model import replies, use_model


def test_no_tracing_without_keys():
    tracing._client.cache_clear()
    assert get_settings().langfuse_public_key == ""
    assert tracing.callbacks() == []


def test_runs_are_traced_when_configured(api, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", SecretStr("sk-lf-test"))
    # Nothing listens here: exporting fails in the background without affecting the run.
    monkeypatch.setattr(settings, "langfuse_base_url", "http://127.0.0.1:9")
    tracing._client.cache_clear()
    handlers = []
    callbacks = tracing.callbacks

    def spy():
        handlers.extend(callbacks())
        return handlers

    monkeypatch.setattr(tracing, "callbacks", spy)
    try:
        agent = make_agent(api, "Traced")
        run = api.post(f"/api/agents/{agent['id']}/runs").json()
        with use_model(replies("ok")):
            resp = api.post(
                f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body("hi")
            )
        assert text_deltas(resp) == "ok"
        assert [type(h) for h in handlers] == [CallbackHandler]
    finally:
        tracing._client.cache_clear()
