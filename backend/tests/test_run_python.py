"""The `run_python` tool, run through agent runs against a fake sandbox backend."""

import asyncio
import json

import pytest
from pydantic import ValidationError
from pydantic_ai import ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from sqlalchemy import select
from test_agents import agent_body, chat_body, make_agent, spy_run_stream

from aios.agent import ChatDeps
from aios.config import get_settings
from aios.models import SandboxSession
from aios.runner import runner
from aios.sandbox import proxy_token, sessions
from aios.sandbox.base import ExecResult, SandboxError, SandboxGone, SandboxHandle, SessionSpec
from aios.tools import TOOLS
from aios.tools.run_python import RunPythonConfig


class FakeBackend:
    def __init__(self) -> None:
        self.specs: list[SessionSpec] = []
        self.calls: list[tuple[str, str, int]] = []
        self.stopped: list[str] = []
        self.gone: set[str] = set()
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
        self.calls.append((handle.ref, code, timeout))
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

    async def model(messages, info: AgentInfo):
        # This turn's tool result, if the tool has been called in it.
        last = messages[-1]
        returns = [p for p in last.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            seen["description"] = info.function_tools[0].description
            yield {0: DeltaToolCall("run_python", json.dumps({"code": code}))}
        else:
            seen["result"] = returns[0].content
            yield "done"

    return FunctionModel(stream_function=model), seen


def python_agent(api, name: str = "Coder", **config) -> tuple[dict, dict]:
    tool = {"key": "run_python", "config": config}
    agent = make_agent(api, name)
    agent = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[tool])).json()
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    return agent, run


def chat(api, agent, run, code: str) -> dict:
    model, seen = run_code(code)
    with runner.override(model=model):
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
    spy = spy_run_stream(monkeypatch)
    seen = chat(api, agent, run, "print('hi')")
    assert spy["deps"] == ChatDeps(user_id=spy["deps"].user_id, run_id=run["id"])
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

    # Deleting a run deletes its session row (the sandbox stops itself when idle).
    api.delete(f"/api/agents/{agent['id']}/runs/{run['id']}")
    db.expire_all()
    assert db.scalar(select(SandboxSession).where(SandboxSession.agent_run_id == run["id"])) is None


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

    # No allowed domains, no network: the sandbox gets no proxy at all.
    agent, run = python_agent(api, "Offline")
    chat(api, agent, run, "print(1)")
    assert backend.specs[1].proxy_url == ""


def test_the_sandbox_is_prewarmed_when_a_turn_starts(api, backend):
    agent, run = python_agent(api)

    async def model(messages, info: AgentInfo):
        # Replies without calling the tool, once the background start has happened.
        for _ in range(500):
            if backend.specs:
                break
            await asyncio.sleep(0.01)
        yield "no code needed"

    with runner.override(model=FunctionModel(stream_function=model)):
        url = f"/api/agents/{agent['id']}/runs/{run['id']}/chat"
        assert api.post(url, json=chat_body("hello")).status_code == 200
    assert [s.session_key for s in backend.specs] == [f"agent-run-{run['id']}"]
    assert backend.calls == []


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
    build = TOOLS["run_python"].build(RunPythonConfig())
    tool = build.toolset.tools["run_python"]

    class Ctx:
        deps = ChatDeps(user_id=1)

    assert asyncio.run(tool.function(Ctx(), "print(1)")) == {
        "error": "Running Python is only available in agent runs."
    }
    assert backend.specs == []
