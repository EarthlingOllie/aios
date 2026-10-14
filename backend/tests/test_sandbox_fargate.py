"""The Fargate backend, against a stubbed ECS API and a mocked runner."""

import asyncio
import json
import threading

import anyio
import boto3
import httpx
import pytest
from botocore.exceptions import NoCredentialsError
from botocore.stub import ANY, Stubber

from aios.config import Settings
from aios.sandbox import runner as runner_module
from aios.sandbox.base import SandboxError, SandboxGone, SandboxHandle, SessionSpec
from aios.sandbox.fargate import DevBackend, FargateBackend, RunnerClient

ARN = "arn:aws:ecs:us-east-1:123456789012:task/aios/abc123"
SPEC = SessionSpec(
    session_key="agent-run-42",
    proxy_url="http://sandbox:tok@proxy.aios.internal:3128",
    idle_seconds=900,
    max_seconds=7200,
)
SETTINGS = Settings(
    sandbox_backend="fargate",
    sandbox_cluster="aios",
    sandbox_task_definition="aios-sandbox",
    sandbox_subnets="subnet-a,subnet-b",
    sandbox_security_groups="sg-sandbox",
    sandbox_start_timeout=5,
)
RESULT = {
    "exit_code": 0,
    "stdout": "4\n",
    "stderr": "",
    "timed_out": False,
    "truncated": False,
    "duration_ms": 12,
}


class FakeRunner:
    """A runner's HTTP API, as an httpx transport."""

    def __init__(self, healthy_after: int = 0, status: int = 200) -> None:
        self.health_checks = 0
        self.healthy_after = healthy_after
        self.status = status
        self.requests: list[httpx.Request] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("unreachable", request=request)
        if request.url.path == "/health":
            self.health_checks += 1
            ok = self.health_checks > self.healthy_after
            return httpx.Response(200 if ok else 503, json={"ok": ok})
        return httpx.Response(self.status, json=RESULT)

    def client(self) -> RunnerClient:
        return RunnerClient(httpx.AsyncClient(transport=httpx.MockTransport(self)))


def task(status: str, ip: str | None = "10.40.20.7", **extra) -> dict:
    details = [{"name": "privateIPv4Address", "value": ip}] if ip else []
    return {
        "taskArn": ARN,
        "lastStatus": status,
        "attachments": [{"type": "ElasticNetworkInterface", "details": details}],
        **extra,
    }


@pytest.fixture
def ecs():
    client = boto3.client(
        "ecs", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def run_task_params() -> dict:
    return {
        "cluster": "aios",
        "taskDefinition": "aios-sandbox",
        "launchType": "FARGATE",
        "count": 1,
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": ["subnet-a", "subnet-b"],
                "securityGroups": ["sg-sandbox"],
                "assignPublicIp": "DISABLED",
            }
        },
        "overrides": ANY,
        "startedBy": "agent-run-42",
        "tags": [{"key": "aios:session", "value": "agent-run-42"}],
    }


def test_start_runs_a_task_and_waits_for_its_runner(ecs):
    client, stubber = ecs
    stubber.add_response("run_task", {"tasks": [task("PROVISIONING", ip=None)]}, run_task_params())
    describe = {"cluster": "aios", "tasks": [ARN]}
    stubber.add_response("describe_tasks", {"tasks": [task("PENDING", ip=None)]}, describe)
    stubber.add_response("describe_tasks", {"tasks": [task("RUNNING")]}, describe)
    runner = FakeRunner(healthy_after=1)
    backend = FargateBackend(SETTINGS, ecs=client, runner=runner.client(), poll_interval=0)

    handle = asyncio.run(backend.start(SPEC))
    assert (handle.ref, handle.endpoint) == (ARN, "http://10.40.20.7:8080")
    assert len(handle.secret) > 30
    assert runner.health_checks == 2


def test_start_passes_the_session_to_the_task(ecs):
    # The session's token, proxy and lifetimes reach the task as environment overrides.
    client, stubber = ecs
    sent = {}
    client.meta.events.register(
        "provide-client-params.ecs.RunTask", lambda params, **_: sent.update(params)
    )
    stubber.add_response("run_task", {"tasks": [task("PROVISIONING", ip=None)]}, run_task_params())
    stubber.add_response("describe_tasks", {"tasks": [task("RUNNING")]})
    backend = FargateBackend(SETTINGS, ecs=client, runner=FakeRunner().client(), poll_interval=0)
    handle = asyncio.run(backend.start(SPEC))

    [container] = sent["overrides"]["containerOverrides"]
    env = {e["name"]: e["value"] for e in container["environment"]}
    assert container["name"] == "sandbox"
    assert env == {
        "SANDBOX_TOKEN": handle.secret,
        "SANDBOX_PROXY_URL": SPEC.proxy_url,
        "SANDBOX_IDLE_SECONDS": "900",
        "SANDBOX_MAX_SECONDS": "7200",
    }


def test_start_reports_ecs_failures(ecs):
    client, stubber = ecs
    failures = [{"arn": "x", "reason": "RESOURCE:ENI"}]
    stubber.add_response("run_task", {"tasks": [], "failures": failures})
    backend = FargateBackend(SETTINGS, ecs=client, runner=FakeRunner().client(), poll_interval=0)
    with pytest.raises(SandboxError, match=r"Couldn't start a sandbox \(RESOURCE:ENI\)"):
        asyncio.run(backend.start(SPEC))


def test_aws_errors_from_run_task_are_sandbox_errors(ecs):
    # Throttling, access denied, no capacity...: the tool reports them to the model.
    client, stubber = ecs
    stubber.add_client_error("run_task", "ThrottlingException", "Rate exceeded")
    backend = FargateBackend(SETTINGS, ecs=client, runner=FakeRunner().client(), poll_interval=0)
    with pytest.raises(SandboxError, match=r"sandbox service failed \(ThrottlingException\)"):
        asyncio.run(backend.start(SPEC))


def test_missing_credentials_are_a_sandbox_error():
    class NoCredentials:
        def run_task(self, **_):
            raise NoCredentialsError()

    backend = FargateBackend(SETTINGS, ecs=NoCredentials(), runner=FakeRunner().client())
    with pytest.raises(SandboxError, match="NoCredentialsError"):
        asyncio.run(backend.start(SPEC))


def test_a_missing_region_is_a_sandbox_error(monkeypatch):
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    with pytest.raises(SandboxError, match="NoRegionError"):
        FargateBackend(SETTINGS)


def test_a_failed_describe_still_stops_the_task(ecs):
    client, stubber = ecs
    stubber.add_response("run_task", {"tasks": [task("PROVISIONING", ip=None)]})
    stubber.add_client_error("describe_tasks", "AccessDeniedException", "User: arn:aws:iam::1:x")
    stop = {"cluster": "aios", "task": ARN, "reason": ANY}
    stubber.add_response("stop_task", {"task": task("STOPPING")}, stop)
    backend = FargateBackend(SETTINGS, ecs=client, runner=FakeRunner().client(), poll_interval=0)
    with pytest.raises(SandboxError) as info:
        asyncio.run(backend.start(SPEC))
    # The AWS message (which can name roles and accounts) stays out of what the model sees.
    assert str(info.value) == "The sandbox service failed (AccessDeniedException)."


def test_a_task_that_stops_while_starting_is_reported(ecs):
    client, stubber = ecs
    stubber.add_response("run_task", {"tasks": [task("PROVISIONING", ip=None)]})
    stopped = task("STOPPED", stoppedReason="CannotPullContainerError")
    stubber.add_response("describe_tasks", {"tasks": [stopped]})
    stop = {"cluster": "aios", "task": ARN, "reason": ANY}
    stubber.add_response("stop_task", {"task": stopped}, stop)
    backend = FargateBackend(SETTINGS, ecs=client, runner=FakeRunner().client(), poll_interval=0)
    with pytest.raises(SandboxError, match="CannotPullContainerError"):
        asyncio.run(backend.start(SPEC))


def test_a_runner_that_never_answers_is_stopped(ecs):
    client, stubber = ecs
    stubber.add_response("run_task", {"tasks": [task("PROVISIONING", ip=None)]})
    stubber.add_response("describe_tasks", {"tasks": [task("RUNNING")]})
    stubber.add_response("stop_task", {"task": task("STOPPING")})
    settings = SETTINGS.model_copy(update={"sandbox_start_timeout": 0})
    runner = FakeRunner(healthy_after=1_000_000)
    backend = FargateBackend(settings, ecs=client, runner=runner.client(), poll_interval=0)
    with pytest.raises(SandboxError, match="didn't respond in time"):
        asyncio.run(backend.start(SPEC))


def test_a_cancelled_start_still_stops_its_task(ecs):
    client, stubber = ecs
    stubber.add_response("run_task", {"tasks": [task("PROVISIONING", ip=None)]})
    stubber.add_response("describe_tasks", {"tasks": [task("RUNNING")]})
    stubber.add_response(
        "stop_task", {"task": task("STOPPING")}, {"cluster": "aios", "task": ARN, "reason": ANY}
    )
    runner = FakeRunner(healthy_after=1_000_000)
    backend = FargateBackend(SETTINGS, ecs=client, runner=runner.client(), poll_interval=0.01)

    async def scenario():
        with anyio.move_on_after(0.2):
            await backend.start(SPEC)

    anyio.run(scenario)


def test_execute_calls_the_runner(ecs):
    client, _ = ecs
    runner = FakeRunner()
    backend = FargateBackend(SETTINGS, ecs=client, runner=runner.client())
    handle = SandboxHandle(ARN, "http://10.40.20.7:8080", "tok")
    result = asyncio.run(backend.execute(handle, "print(2 + 2)", 30))
    assert result.stdout == "4\n"
    [request] = runner.requests
    assert str(request.url) == "http://10.40.20.7:8080/exec"
    assert request.headers["Authorization"] == "Bearer tok"
    assert json.loads(request.content) == {"code": "print(2 + 2)", "timeout": 30}
    assert asyncio.run(backend.alive(handle))


def test_a_stopped_task_is_gone(ecs):
    client, _ = ecs
    handle = SandboxHandle(ARN, "http://10.40.20.7:8080", "tok")
    # Nothing at the address any more, or another session's sandbox has it.
    runner = FakeRunner()
    runner.down = True
    backend = FargateBackend(SETTINGS, ecs=client, runner=runner.client())
    with pytest.raises(SandboxGone):
        asyncio.run(backend.execute(handle, "1", 5))
    assert not asyncio.run(backend.alive(handle))
    backend = FargateBackend(SETTINGS, ecs=client, runner=FakeRunner(status=401).client())
    with pytest.raises(SandboxGone):
        asyncio.run(backend.execute(handle, "1", 5))
    backend = FargateBackend(SETTINGS, ecs=client, runner=FakeRunner(status=500).client())
    with pytest.raises(SandboxError, match="refused the call"):
        asyncio.run(backend.execute(handle, "1", 5))


def test_an_unreadable_result_is_a_sandbox_error(ecs):
    client, _ = ecs
    handle = SandboxHandle(ARN, "http://10.40.20.7:8080", "tok")
    for body in (b"not json", b'{"stdout": 4}'):
        transport = httpx.MockTransport(lambda _, body=body: httpx.Response(200, content=body))
        runner = RunnerClient(httpx.AsyncClient(transport=transport))
        backend = FargateBackend(SETTINGS, ecs=client, runner=runner)
        with pytest.raises(SandboxError, match="couldn't be read"):
            asyncio.run(backend.execute(handle, "1", 5))


def test_stop_is_best_effort(ecs):
    client, stubber = ecs
    stubber.add_client_error("stop_task", "InvalidParameterException")
    backend = FargateBackend(SETTINGS, ecs=client, runner=FakeRunner().client())
    asyncio.run(backend.stop(SandboxHandle(ARN, "", "")))


def test_dev_backend_shares_one_runner():
    settings = Settings(sandbox_dev_url="http://sandbox:8080/", sandbox_dev_token="dev")
    runner = FakeRunner()
    backend = DevBackend(settings, runner=runner.client())
    # The runner's network is open, so allowed domains don't need SANDBOX_PROXY_* set.
    assert backend.restricts_network is False
    handle = asyncio.run(backend.start(SPEC))
    assert handle == SandboxHandle("dev", "http://sandbox:8080", "dev")
    assert asyncio.run(backend.execute(handle, "print(4)", 5)).stdout == "4\n"
    runner.down = True
    with pytest.raises(SandboxError, match="isn't running"):
        asyncio.run(backend.start(SPEC))


def test_the_runner_client_against_a_real_runner(workdir):
    # The same HTTP path a Fargate task serves, with the real runner behind it.
    server = runner_module.Server(("127.0.0.1", 0), "dev", idle_seconds=60, max_seconds=600)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = Settings(
        sandbox_dev_url=f"http://127.0.0.1:{server.server_address[1]}", sandbox_dev_token="dev"
    )
    backend = DevBackend(settings)

    async def scenario():
        handle = await backend.start(SPEC)
        await backend.execute(handle, "open('kept.txt', 'w').write('from call one')", 10)
        return await backend.execute(handle, "print(open('kept.txt').read())", 10)

    try:
        result = asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
    assert (result.exit_code, result.stdout) == (0, "from call one\n")
