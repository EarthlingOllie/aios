"""The `run_python` tool, run through agent runs against a fake sandbox backend."""

import asyncio
import time
import uuid
from datetime import timedelta

import pytest
from conftest import user_store
from pydantic import ValidationError
from sqlalchemy import select
from test_agents import agent_body, chat_body, make_agent, spy_run

from aios.agent import ChatDeps
from aios.config import get_settings
from aios.datastores.models import AgentVersionTool
from aios.models import SandboxSession
from aios.sandbox import proxy_token, sessions
from aios.sandbox.base import (
    ExecResult,
    SandboxError,
    SandboxGone,
    SandboxHandle,
    SandboxNotReached,
    SessionSpec,
)
from aios.testing.fake_model import (
    ToolCall,
    last_tool_results,
    model,
    replies,
    tool_output,
    use_model,
)
from aios.tools import TOOLS
from aios.tools.run_python import RunPythonConfig


class FakeBackend:
    def __init__(self) -> None:
        self.specs: list[SessionSpec] = []
        self.calls: list[tuple[str, str, int]] = []
        self.stopped: list[str] = []
        self.gone: set[str] = set()
        # Pass the alive check, then stop before the call reaches them (e.g. at the idle limit).
        self.vanishing: set[str] = set()
        self.fail_start = False

    async def start(self, spec: SessionSpec) -> SandboxHandle:
        if self.fail_start:
            raise SandboxError("No capacity right now.")
        self.specs.append(spec)
        n = len(self.specs)
        return SandboxHandle(f"sandbox-{n}", f"http://10.0.0.{n}:8080", f"secret-{n}")

    async def alive(self, handle: SandboxHandle) -> bool:
        return handle.ref not in self.gone

    async def execute(self, handle: SandboxHandle, code: str, timeout: int) -> ExecResult:
        if handle.ref in self.gone:
            raise SandboxGone("stopped")
        if handle.ref in self.vanishing:
            self.gone.add(handle.ref)
            raise SandboxNotReached("unreachable")
        self.calls.append((handle.ref, code, timeout))
        if "crash" in code:
            # Takes the sandbox down partway through, like running out of memory.
            self.gone.add(handle.ref)
            raise SandboxGone("stopped")
        return ExecResult(
            exit_code=0,
            stdout=f"ran in {handle.ref}\n",
            stderr="",
            timed_out=False,
            truncated=False,
            duration_ms=5,
        )

    async def stop(self, handle: SandboxHandle) -> None:
        self.stopped.append(handle.ref)


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(sessions, "get_backend", lambda: fake)
    return fake


async def noop_ensure(**kwargs) -> None:
    pass


def run_code(code: str):
    """A model that calls `run_python` once with `code`, then replies with what came back."""
    seen = {}

    async def script(messages, info):
        # This turn's tool result, if the tool has been called in it.
        if returns := last_tool_results(messages):
            seen["result"] = tool_output(returns[0])
            yield "done"
        else:
            seen["description"] = info.tools["run_python"].description
            yield ToolCall("run_python", {"code": code})

    return model(script), seen


def python_agent(api, name: str = "Coder", **config) -> tuple[dict, dict]:
    tool = {"key": "run_python", "config": config}
    agent = make_agent(api, name)
    agent = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[tool])).json()
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    return agent, run


def chat(api, agent, run, code: str) -> dict:
    fake, seen = run_code(code)
    with use_model(fake):
        url = f"/api/agents/{agent['id']}/runs/{run['id']}/chat"
        assert api.post(url, json=chat_body("run it")).status_code == 200
    return seen


def test_run_python_settings():
    domains = [" API.Example.com ", "", "api.example.com", "*.data.gov"]
    config = RunPythonConfig(allowed_domains=domains)
    assert config.allowed_domains == ["api.example.com", "*.data.gov"]
    bad_domains = ["https://a.com", "a.com/path", "a.com:443", "localhost", "*", "a.*.com", "*.gov"]
    for bad in bad_domains:
        with pytest.raises(ValidationError, match="isn't a domain"):
            RunPythonConfig(allowed_domains=[bad])
    for bad in [0, 301]:
        with pytest.raises(ValidationError):
            RunPythonConfig(timeout_seconds=bad)
    with pytest.raises(ValidationError):
        RunPythonConfig(unknown=1)


def test_run_python_is_in_the_catalog(api):
    tools = {t["key"]: t for t in api.get("/api/tools").json()}
    assert tools["run_python"]["default_config"] == {"timeout_seconds": 60, "allowed_domains": []}
    assert TOOLS["run_python"].name == "Run Python"


def test_one_sandbox_per_run(api, backend, db, monkeypatch):
    agent, run = python_agent(api, timeout_seconds=30)
    spy = spy_run(monkeypatch)
    seen = chat(api, agent, run, "print('hi')")
    assert spy["deps"].run_id == run["id"]
    assert seen["result"] == {
        "exit_code": 0,
        "stdout": "ran in sandbox-1\n",
        "stderr": "",
        "timed_out": False,
        "truncated": False,
        "duration_ms": 5,
    }
    # The model is told what the sandbox can do.
    assert "/work, persists" in seen["description"]
    assert "no network access" in seen["description"]
    assert "30 seconds" in seen["description"]

    # The next turn reuses the run's sandbox.
    chat(api, agent, run, "print('again')")
    assert backend.calls == [
        ("sandbox-1", "print('hi')", 30),
        ("sandbox-1", "print('again')", 30),
    ]
    assert len(backend.specs) == 1
    assert backend.specs[0].session_key == f"agent-run-{run['id']}"
    row = db.scalar(select(SandboxSession).where(SandboxSession.agent_run_id == run["id"]))
    assert (row.ref, row.endpoint, row.secret) == ("sandbox-1", "http://10.0.0.1:8080", "secret-1")

    # Another run of the same agent gets its own.
    other = api.post(f"/api/agents/{agent['id']}/runs").json()
    chat(api, agent, other, "print('elsewhere')")
    assert backend.calls[-1][0] == "sandbox-2"

    # Deleting a run deletes its session row, and stops its sandbox.
    api.delete(f"/api/agents/{agent['id']}/runs/{run['id']}")
    db.expire_all()
    assert db.scalar(select(SandboxSession).where(SandboxSession.agent_run_id == run["id"])) is None
    assert backend.stopped == ["sandbox-1"]
    # Archiving the agent leaves its other runs' sandboxes be: nothing is thrown away.
    api.post(f"/api/agents/{agent['id']}/archive")
    assert backend.stopped == ["sandbox-1"]


def test_a_sandbox_that_has_gone_is_replaced(api, backend, monkeypatch):
    # Without pre-warming: found gone by the call itself.
    monkeypatch.setattr(sessions.manager, "ensure", noop_ensure)
    agent, run = python_agent(api)
    chat(api, agent, run, "open('f', 'w')")
    backend.gone.add("sandbox-1")
    seen = chat(api, agent, run, "open('f')")
    assert seen["result"]["stdout"] == "ran in sandbox-2\n"
    # The model is told /work was reset, once.
    assert seen["result"]["sandbox_restarted"] is True
    assert len(backend.specs) == 2
    assert "sandbox_restarted" not in chat(api, agent, run, "print(1)")["result"]
    # The old one is stopped, in case it was only unreachable for a while.
    assert backend.stopped == ["sandbox-1"]


def test_code_is_not_run_twice_when_the_sandbox_stops_during_a_call(api, backend, monkeypatch):
    monkeypatch.setattr(sessions.manager, "ensure", noop_ensure)
    agent, run = python_agent(api)
    seen = chat(api, agent, run, "crash()")
    # Not retried in a new sandbox: the code may have partly run, e.g. made a request.
    assert [ref for ref, _, _ in backend.calls] == ["sandbox-1"]
    assert "may have partly run" in seen["result"]["error"]
    assert len(backend.specs) == 1
    # The next call gets a new sandbox, and is told /work was reset.
    seen = chat(api, agent, run, "print(1)")
    assert seen["result"]["stdout"] == "ran in sandbox-2\n"
    assert seen["result"]["sandbox_restarted"] is True


def test_code_runs_in_a_new_sandbox_when_the_call_never_reached_the_old_one(
    api, backend, monkeypatch
):
    monkeypatch.setattr(sessions.manager, "ensure", noop_ensure)
    agent, run = python_agent(api)
    chat(api, agent, run, "open('f', 'w')")
    # Alive when checked, stopped by the time the call connects: the code never ran there.
    backend.vanishing.add("sandbox-1")
    seen = chat(api, agent, run, "print(1)")
    assert seen["result"]["stdout"] == "ran in sandbox-2\n"
    assert seen["result"]["sandbox_restarted"] is True
    assert [ref for ref, _, _ in backend.calls] == ["sandbox-1", "sandbox-2"]
    assert backend.stopped == ["sandbox-1"]


def test_code_is_retried_once_when_calls_never_reach_the_sandbox(api, backend, monkeypatch):
    monkeypatch.setattr(sessions.manager, "ensure", noop_ensure)
    agent, run = python_agent(api)
    backend.vanishing.update({"sandbox-1", "sandbox-2"})
    seen = chat(api, agent, run, "print(1)")
    assert seen["result"]["error"] == "unreachable"  # Not "may have partly run".
    assert len(backend.specs) == 2
    assert backend.calls == []
    # The next call starts another.
    assert chat(api, agent, run, "print(1)")["result"]["stdout"] == "ran in sandbox-3\n"


def test_a_sandbox_another_replica_records_first_is_used(api, backend, monkeypatch):
    monkeypatch.setattr(sessions.manager, "ensure", noop_ensure)
    agent, run = python_agent(api)
    start = backend.start

    async def racing_start(spec):
        handle = await start(spec)
        if handle.ref == "sandbox-1":
            # Meanwhile, another replica starts a sandbox for the run and records it.
            other = await start(spec)
            assert sessions._save(run["id"], other, replaced=False, previous=None)
        return handle

    monkeypatch.setattr(backend, "start", racing_start)
    seen = chat(api, agent, run, "print(1)")
    # One sandbox per run: this request's is stopped rather than overwriting the other's.
    assert seen["result"]["stdout"] == "ran in sandbox-2\n"
    assert backend.stopped == ["sandbox-1"]


def _run_id(api) -> int:
    return python_agent(api)[1]["id"]


def test_a_sandbox_that_cant_be_recorded_is_stopped(api, backend, monkeypatch):
    run_id = _run_id(api)

    def unreachable(*args, **kwargs):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(sessions, "_save", unreachable)
    with pytest.raises(RuntimeError):
        asyncio.run(sessions.manager.ensure(run_id=run_id, allowed_domains=[]))
    assert backend.stopped == ["sandbox-1"]


def test_a_start_cancelled_while_recording_keeps_its_sandbox(api, backend, db, monkeypatch):
    run_id = _run_id(api)
    save = sessions._save

    def slow_save(*args, **kwargs):
        time.sleep(0.3)
        return save(*args, **kwargs)

    monkeypatch.setattr(sessions, "_save", slow_save)

    async def scenario():
        task = asyncio.create_task(sessions.manager.ensure(run_id=run_id, allowed_domains=[]))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        while sessions._background:
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    # The save finished, so the sandbox is recorded for the run's next call, not left running
    # with nothing pointing at it.
    row = db.scalar(select(SandboxSession).where(SandboxSession.agent_run_id == run_id))
    assert (row.ref, backend.stopped) == ("sandbox-1", [])


def test_prewarming_replaces_a_sandbox_that_has_gone(api, backend, db):
    agent, run = python_agent(api)
    chat(api, agent, run, "open('f', 'w')")
    backend.gone.add("sandbox-1")
    # The turn's pre-warm finds the sandbox gone and starts another before the model's call,
    # and the call still tells the model /work was reset.
    seen = chat(api, agent, run, "open('f')")
    assert [ref for ref, _, _ in backend.calls] == ["sandbox-1", "sandbox-2"]
    assert seen["result"]["sandbox_restarted"] is True
    assert "sandbox_restarted" not in chat(api, agent, run, "print(1)")["result"]
    assert len(backend.specs) == 2


def test_the_proxy_token_carries_the_allowed_domains(api, backend, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "sandbox_proxy_address", "proxy.aios.internal:3128")
    monkeypatch.setattr(settings, "sandbox_proxy_secret", type(settings.sandbox_proxy_secret)("s"))
    agent, run = python_agent(api, allowed_domains=["api.example.com", "*.data.gov"])
    seen = chat(api, agent, run, "import httpx")
    assert "HTTPS (port 443) to api.example.com, *.data.gov only" in seen["description"]
    proxy_url = backend.specs[0].proxy_url
    prefix, _, address = proxy_url.rpartition("@")
    assert address == "proxy.aios.internal:3128"
    grant = proxy_token.verify("s", prefix.removeprefix("http://sandbox:"))
    assert grant.session == f"agent-run-{run['id']}"
    assert grant.domains == ("api.example.com", "*.data.gov")
    # Valid for the sandbox's whole life, which counts from when it has booted.
    lifetime = settings.sandbox_start_timeout + settings.sandbox_max_session_seconds
    assert grant.expires_at > time.time() + lifetime - 60

    # No allowed domains, no network: the sandbox gets no proxy at all.
    agent, run = python_agent(api, "Offline")
    chat(api, agent, run, "print(1)")
    assert backend.specs[1].proxy_url == ""


def test_allowed_domains_without_a_proxy_are_an_error(api, backend):
    # No SANDBOX_PROXY_ADDRESS or SANDBOX_PROXY_SECRET: the sandbox isn't started without the
    # network the model is told it has.
    agent, run = python_agent(api, allowed_domains=["api.example.com"])
    seen = chat(api, agent, run, "import httpx")
    assert "Network access for the sandbox isn't set up" in seen["result"]["error"]
    assert backend.specs == []


def test_allowed_domains_need_no_proxy_on_a_backend_with_open_network(api, backend):
    # E.g. the dev backend under docker compose: its runner can already reach anything, so no
    # proxy is configured and the sandbox starts without one. Backends that don't say so (like
    # FakeBackend in the other tests) are assumed to restrict it, and need the proxy.
    backend.restricts_network = False
    agent, run = python_agent(api, allowed_domains=["api.github.com"])
    seen = chat(api, agent, run, "import httpx")
    assert seen["result"]["stdout"] == "ran in sandbox-1\n"
    assert backend.specs[0].proxy_url == ""


def test_the_sandbox_is_prewarmed_when_a_turn_starts(api, backend):
    agent, run = python_agent(api)

    async def script(messages, info):
        # Replies without calling the tool, once the background start has happened.
        for _ in range(500):
            if backend.specs:
                break
            await asyncio.sleep(0.01)
        yield "no code needed"

    with use_model(model(script)):
        url = f"/api/agents/{agent['id']}/runs/{run['id']}/chat"
        assert api.post(url, json=chat_body("hello")).status_code == 200
    assert [s.session_key for s in backend.specs] == [f"agent-run-{run['id']}"]
    assert backend.calls == []


def test_prewarming_uses_the_settings_the_tool_is_built_with(api, backend, user, monkeypatch):
    agent, run = python_agent(api, allowed_domains=["api.example.com"])
    # Stored by an older version of the settings model: not in today's canonical form.
    with user_store(user) as store:
        tool = store.scalar(
            select(AgentVersionTool).where(
                AgentVersionTool.agent_version_id == agent["current_version"]["id"]
            )
        )
        tool.config = {"allowed_domains": ["API.Example.com."], "dropped_setting": 1}
        store.commit()
    prewarmed = []

    async def ensure(*, run_id, allowed_domains):
        prewarmed.append(allowed_domains)

    monkeypatch.setattr(sessions.manager, "ensure", ensure)
    fake, _ = run_code("print(1)")
    with use_model(fake):
        url = f"/api/agents/{agent['id']}/runs/{run['id']}/chat"
        assert api.post(url, json=chat_body("run it")).status_code == 200
    assert prewarmed == [["api.example.com"]]


def test_later_turns_prewarm_only_while_the_run_uses_its_sandbox(api, backend, db, monkeypatch):
    prewarmed = []

    async def ensure(*, run_id, allowed_domains):
        prewarmed.append(run_id)

    monkeypatch.setattr(sessions.manager, "ensure", ensure)

    def reply(run):
        with use_model(replies("no code needed")):
            url = f"/api/agents/{agent['id']}/runs/{run['id']}/chat"
            assert api.post(url, json=chat_body("hello")).status_code == 200

    agent, run = python_agent(api)
    # The first turn pre-warms; a later one doesn't if the run hasn't used its sandbox.
    reply(run)
    reply(run)
    assert prewarmed == [run["id"]]
    # Once it has, later turns pre-warm...
    chat(api, agent, run, "print(1)")
    prewarmed.clear()
    reply(run)
    assert prewarmed == [run["id"]]
    # ...until it goes unused for long enough to have stopped as idle.
    row = db.scalar(select(SandboxSession).where(SandboxSession.agent_run_id == run["id"]))
    row.last_used_at -= timedelta(seconds=get_settings().sandbox_idle_seconds + 1)
    db.commit()
    prewarmed.clear()
    reply(run)
    assert prewarmed == []


def test_sandbox_errors_go_to_the_model(api, backend):
    agent, run = python_agent(api)
    backend.fail_start = True
    seen = chat(api, agent, run, "print(1)")
    assert seen["result"] == {"error": "No capacity right now."}


def test_without_a_backend_the_tool_says_so(api, monkeypatch):
    monkeypatch.setattr(sessions, "get_backend", lambda: None)
    agent, run = python_agent(api)
    seen = chat(api, agent, run, "print(1)")
    assert seen["result"] == {"error": "Running Python isn't set up on this server."}


def test_only_agent_runs_have_a_sandbox(backend):
    [tool] = TOOLS["run_python"].build(RunPythonConfig())

    class Runtime:
        context = ChatDeps(user_id=1, data_store_id=uuid.uuid4())

    assert asyncio.run(tool.coroutine("print(1)", Runtime())) == {
        "error": "Running Python is only available in agent runs."
    }
    assert backend.specs == []
