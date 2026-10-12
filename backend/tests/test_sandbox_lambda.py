"""The Lambda backend (`aios.sandbox.aws_lambda`) with the real handler and runner.

A fake Lambda runs `lambda_handler` in-process, one simulated execution environment per tenant,
and a fake S3 server takes the presigned GETs and POSTs boto3 makes, checking each POST against
its policy.
"""

import base64
import email.parser
import email.policy
import hashlib
import io
import json
import os
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import boto3
import pytest
from botocore.exceptions import ClientError

from aios.config import Settings
from aios.sandbox import aws_lambda, lambda_handler, runner
from aios.sandbox.aws_lambda import LambdaBackend, work_key
from aios.sandbox.base import SandboxError, SandboxGone, SandboxHandle, SessionSpec

BUCKET = "work"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- fake S3 -------------------------------------------------------------------------------------


class FakeS3(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _S3Handler)
        self.objects: dict[str, bytes] = {}
        self.requests: list[tuple[str, int]] = []
        self.refuse_posts = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def methods(self, status: int | None = None) -> list[str]:
        return [m for m, s in self.requests if status is None or s == status]


class _S3Handler(BaseHTTPRequestHandler):
    server: FakeS3

    def _reply(self, status: int, body: bytes = b"", etag: str | None = None) -> None:
        self.server.requests.append((self.command, status))
        self.send_response(status)
        if etag:
            self.send_header("ETag", f'"{etag}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        assert "X-Amz-Signature=" in self.path, "not a presigned URL"
        key = path.removeprefix(f"/{BUCKET}/")
        data = self.server.objects.get(key)
        if data is None:
            return self._reply(404)
        etag = hashlib.md5(data).hexdigest()
        if self.headers.get("If-None-Match") == f'"{etag}"':
            return self._reply(304, etag=etag)
        self._reply(200, data, etag)

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
        )
        fields = {
            part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in message.iter_parts()
        }
        # The file must come last, and the policy must allow it.
        assert list(fields)[-1] == "file"
        policy = json.loads(base64.b64decode(fields["policy"]))
        key, data = fields["key"].decode(), fields["file"]
        for condition in policy["conditions"]:
            if (
                isinstance(condition, list)
                and condition[0] == "content-length-range"
                and not condition[1] <= len(data) <= condition[2]
            ):
                return self._reply(400)
        assert {"bucket": BUCKET} in policy["conditions"]
        assert {"key": key} in policy["conditions"]
        if self.server.refuse_posts:
            return self._reply(503)
        self.server.objects[key] = data
        self._reply(204, etag=hashlib.md5(data).hexdigest())

    def do_DELETE(self) -> None:
        key = unquote(urlsplit(self.path).path).removeprefix(f"/{BUCKET}/")
        self.server.objects.pop(key, None)
        self._reply(204)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def s3():
    server = FakeS3()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


# --- fake Lambda ---------------------------------------------------------------------------------


class _Payload(io.BytesIO):
    pass


class FakeLambda:
    """Runs the handler in-process. Each tenant gets an execution environment of its own (the
    handler's state, swapped in for its calls), as with tenant isolation; `shared` puts every
    call in one environment instead."""

    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch
        self.shared = False
        self.environments: dict[str | None, lambda_handler._State] = {}
        self.invocations: list[tuple[str | None, dict]] = []
        self.lock = threading.Lock()
        self.error: ClientError | None = None
        self.die_after_running = False

    def cold(self) -> None:
        """Every environment has been reclaimed: the next call starts from an empty /tmp."""
        self.environments.clear()
        lambda_handler._empty(self.scratch)

    def invoke(self, *, FunctionName, InvocationType, Payload, TenantId=None):  # noqa: N803
        assert InvocationType == "RequestResponse"
        if self.error:
            raise self.error
        event = json.loads(Payload)
        with self.lock:
            self.invocations.append((TenantId, event))
            if self.shared:
                TenantId = None  # noqa: N806
            if TenantId not in self.environments:
                # A new environment starts with an empty /tmp.
                lambda_handler._empty(self.scratch)
            state = self.environments.setdefault(TenantId, lambda_handler._State())
            saved, lambda_handler._state = lambda_handler._state, state
            try:
                if self.die_after_running:
                    self.die_after_running = False

                    def die(*args):
                        raise SystemExit("Runtime exited")

                    original, lambda_handler._save = lambda_handler._save, die
                    try:
                        lambda_handler.handler(event)
                    finally:
                        lambda_handler._save = original
                result = lambda_handler.handler(event)
            except BaseException as exc:
                # Lambda resets an environment whose runtime failed.
                del self.environments[TenantId]
                body = {"errorMessage": str(exc), "errorType": type(exc).__name__}
                return {
                    "FunctionError": "Unhandled",
                    "Payload": _Payload(json.dumps(body).encode()),
                }
            finally:
                lambda_handler._state = saved
        return {"StatusCode": 200, "Payload": _Payload(json.dumps(result).encode())}

    def tenants(self) -> list[str | None]:
        return [tenant for tenant, _ in self.invocations]


@pytest.fixture
def scratch(monkeypatch):
    # The handler's /tmp. Not under pytest's tmp_path, whose parents only its owner can enter:
    # run as root, the runner runs the code as another user, which needs to reach /work.
    with tempfile.TemporaryDirectory() as parent:
        os.chmod(parent, 0o755)
        monkeypatch.setattr(tempfile, "tempdir", parent)
        monkeypatch.setattr(runner, "WORKDIR", str(Path(parent) / "work"))
        monkeypatch.setattr(lambda_handler, "_state", lambda_handler._State())
        yield Path(parent)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def settings(**overrides) -> Settings:
    values = {
        "sandbox_backend": "lambda",
        "sandbox_lambda_function": "aios-sandbox",
        "sandbox_work_bucket": BUCKET,
        **overrides,
    }
    return Settings(**values)


def s3_client(s3: FakeS3):
    return boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url=s3.url,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=boto3.session.Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def lam(scratch):
    return FakeLambda(scratch)


def make_backend(s3, lam, clock, **overrides) -> LambdaBackend:
    return LambdaBackend(
        settings(**overrides), lambda_client=lam, s3_client=s3_client(s3), clock=clock
    )


@pytest.fixture
def backend(s3, lam, clock):
    return make_backend(s3, lam, clock)


SPEC = SessionSpec(session_key="agent-run-1", proxy_url="", idle_seconds=900, max_seconds=7200)


async def run(backend: LambdaBackend, handle: SandboxHandle, code: str, timeout: int = 30):
    return await backend.execute(handle, code, timeout)


# --- tests ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_files_in_work_carry_over_between_calls(backend, lam, s3):
    handle = await backend.start(SPEC)
    # The start warmed an environment for the session's own tenant.
    assert lam.invocations == [(handle.ref, {"ping": True})]

    first = await run(backend, handle, "open('notes.txt', 'w').write('hello')\nprint('saved')")
    assert (first.exit_code, first.stdout) == (0, "saved\n")
    assert work_key(handle.ref) in s3.objects

    second = await run(backend, handle, "import os; print(os.getcwd(), open('notes.txt').read())")
    assert second.stdout.split()[1] == "hello"
    # Every call went to the session's tenant, and the warm environment's /work was current, so
    # the second call didn't download it again; nor upload it, as it didn't change.
    assert lam.tenants() == [handle.ref] * 3
    assert s3.methods(200) == []
    assert s3.methods(304) == ["GET"]
    assert s3.methods(204) == ["POST"]


@pytest.mark.anyio
async def test_a_cold_environment_restores_work_from_s3(backend, lam, s3):
    handle = await backend.start(SPEC)
    await run(
        backend, handle, "import os; os.makedirs('data'); open('data/x.csv', 'w').write('1,2')"
    )
    lam.cold()

    result = await run(backend, handle, "print(open('data/x.csv').read())")
    assert result.stdout == "1,2\n"
    assert s3.methods(200) == ["GET"]


@pytest.mark.anyio
async def test_a_failed_call_leaves_work_as_it_was(backend, lam):
    handle = await backend.start(SPEC)
    await run(backend, handle, "open('n', 'w').write('1')")

    # The code runs, then the runtime dies before /work is saved (as when the function runs out of
    # memory or time): the environment is reset.
    lam.die_after_running = True
    with pytest.raises(SandboxError) as caught:
        await run(backend, handle, "open('n', 'w').write('2')")
    assert not isinstance(caught.value, SandboxGone)
    assert str(caught.value) == aws_lambda.FAILED_CALL

    result = await run(backend, handle, "print(open('n').read())")
    assert result.stdout == "1\n"


@pytest.mark.anyio
async def test_work_over_the_limit_isnt_kept(s3, lam, clock):
    backend = make_backend(s3, lam, clock, sandbox_work_limit_mb=1)
    handle = await backend.start(SPEC)
    await run(backend, handle, "open('small', 'w').write('kept')")

    result = await run(backend, handle, "open('big', 'wb').write(b'x' * 2_000_000)")
    assert result.exit_code == 0
    assert "over the 1 MB limit, so this call's changes to it weren't kept" in result.stderr

    # The same warm environment goes back to the saved /work.
    result = await run(backend, handle, "import os; print(sorted(os.listdir()))")
    assert result.stdout == "['small']\n"


@pytest.mark.anyio
async def test_work_that_cant_be_saved_isnt_kept(backend, lam, s3):
    handle = await backend.start(SPEC)
    await run(backend, handle, "open('a', 'w').write('1')")

    s3.refuse_posts = True
    result = await run(backend, handle, "open('b', 'w').write('2')")
    assert "/work couldn't be saved (HTTPError)" in result.stderr

    s3.refuse_posts = False
    result = await run(backend, handle, "import os; print(sorted(os.listdir()))")
    assert result.stdout == "['a']\n"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "replace",
    [
        "shutil.rmtree(os.getcwd())",
        "os.chdir('..'); os.rename('work', 'elsewhere'); os.symlink('elsewhere', 'work')",
        "os.chdir('..'); shutil.rmtree('work'); open('work', 'w').write('a file')",
    ],
)
async def test_code_that_removes_or_replaces_work_leaves_an_empty_one(
    backend, lam, scratch, replace
):
    handle = await backend.start(SPEC)
    await run(backend, handle, "open('kept', 'w').write('1')")

    result = await run(backend, handle, f"import os, shutil\n{replace}")
    assert (result.exit_code, result.stderr) == (0, "")
    work = scratch / "work"
    assert work.is_dir() and not work.is_symlink()

    lam.cold()
    result = await run(backend, handle, "import os; print(os.listdir())")
    assert result.stdout == "[]\n"


def _archive(entries: list[tuple[tarfile.TarInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for info, data in entries:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data else None)
    return buffer.getvalue()


@pytest.mark.anyio
async def test_a_restore_only_writes_inside_work(backend, s3, scratch):
    handle = await backend.start(SPEC)
    escape = tarfile.TarInfo("../escaped")
    link = tarfile.TarInfo("passwd")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    fifo = tarfile.TarInfo("pipe")
    fifo.type = tarfile.FIFOTYPE
    s3.objects[work_key(handle.ref)] = _archive(
        [(tarfile.TarInfo("ok.txt"), b"fine"), (escape, b"x"), (link, b""), (fifo, b"")]
    )

    result = await run(backend, handle, "import os; print(sorted(os.listdir()))")
    assert result.stdout == "['ok.txt']\n"
    assert not (scratch / "escaped").exists()


@pytest.mark.anyio
async def test_another_sessions_files_are_cleared(backend, lam):
    # Should an environment ever serve two sessions, the second doesn't see the first's files.
    lam.shared = True
    first = await backend.start(SPEC)
    # `..` is /tmp in the image (/work is /tmp/work).
    await run(backend, first, "open('../scratch', 'w').write('1'); open('mine', 'w').write('1')")

    second = await backend.start(SPEC)
    result = await run(
        backend, second, "import os; print(sorted(os.listdir()), os.path.exists('../scratch'))"
    )
    assert lam.tenants() == [first.ref, first.ref, second.ref, second.ref]
    assert result.stdout == "[] False\n"

    # The first session's /work is still in S3.
    result = await run(backend, first, "print(open('mine').read())")
    assert result.stdout == "1\n"


@pytest.mark.anyio
async def test_the_code_gets_the_sessions_proxy(backend):
    proxied = await backend.start(
        SessionSpec("agent-run-1", "http://sandbox:tok@proxy:3128", 900, 7200)
    )
    assert proxied.secret == "http://sandbox:tok@proxy:3128"
    code = "import os; print(os.environ.get('HTTPS_PROXY'))"
    assert (await run(backend, proxied, code)).stdout == "http://sandbox:tok@proxy:3128\n"

    offline = await backend.start(SPEC)
    assert (await run(backend, offline, code)).stdout == "None\n"


@pytest.mark.anyio
async def test_a_session_ends_at_its_maximum_lifetime(backend, clock, s3):
    handle = await backend.start(SPEC)
    await run(backend, handle, "print(1)")
    # Idle time doesn't matter: /work is kept in S3 meanwhile.
    clock.now += SPEC.max_seconds - 1
    assert await backend.alive(handle)
    assert (await run(backend, handle, "print(2)")).stdout == "2\n"

    clock.now += 1
    assert not await backend.alive(handle)
    with pytest.raises(SandboxGone):
        await run(backend, handle, "print(3)")

    await backend.stop(handle)
    assert work_key(handle.ref) not in s3.objects
    # Nothing left to delete is fine too, as is a handle that isn't ours.
    await backend.stop(handle)
    assert not await backend.alive(SandboxHandle("x", "elsewhere", ""))


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "arn:aws:iam::123:role/x"}}, "Invoke")


@pytest.mark.anyio
async def test_aws_errors_go_to_the_model_without_details(backend, lam):
    lam.error = _client_error("TooManyRequestsException")
    with pytest.raises(SandboxError, match="busy"):
        await backend.start(SPEC)

    lam.error = _client_error("AccessDeniedException")
    with pytest.raises(SandboxError) as caught:
        await backend.start(SPEC)
    assert str(caught.value) == "The sandbox service failed (AccessDeniedException)."


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer, message",
    [
        ({"error": "Couldn't restore /work (500), so the code wasn't run."}, "Couldn't restore"),
        ({"result": {"stdout": 1}}, "couldn't be read"),
        ([], "couldn't be read"),
        ("not json", "couldn't be read"),
    ],
)
@pytest.mark.anyio
async def test_bad_answers_are_sandbox_errors(backend, lam, monkeypatch, answer, message):
    handle = await backend.start(SPEC)

    def invoke(**kwargs):
        payload = answer.encode() if isinstance(answer, str) else json.dumps(answer).encode()
        return {"StatusCode": 200, "Payload": _Payload(payload)}

    monkeypatch.setattr(lam, "invoke", invoke)
    with pytest.raises(SandboxError, match=message) as caught:
        await run(backend, handle, "print(1)")
    assert not isinstance(caught.value, SandboxGone)


@pytest.mark.anyio
async def test_a_failed_ping_is_a_failed_start(backend, lam, monkeypatch):
    def invoke(**kwargs):
        return {"FunctionError": "Unhandled", "Payload": _Payload(b"{}")}

    monkeypatch.setattr(lam, "invoke", invoke)
    with pytest.raises(SandboxError, match="Couldn't start a sandbox"):
        await backend.start(SPEC)


def test_needs_a_function_and_a_bucket():
    with pytest.raises(SandboxError, match="isn't set up"):
        LambdaBackend(settings(sandbox_work_bucket=""))


def test_real_clients_never_retry_an_invocation(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
    # Presigning needs credentials, though nothing is sent.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    backend = LambdaBackend(settings())
    assert backend.lam.meta.config.retries["total_max_attempts"] == 1
    assert backend.lam.meta.config.read_timeout >= 15 * 60
    # Presigned for the regional endpoint, which the sandbox subnets reach through S3's gateway
    # endpoint, as `<bucket>.s3.<region>.amazonaws.com`.
    url = backend.s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": "k"}, ExpiresIn=60
    )
    assert url.startswith(f"https://{BUCKET}.s3.eu-west-2.amazonaws.com/k?")


def test_without_a_region_it_isnt_set_up(monkeypatch):
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    with pytest.raises(SandboxError, match="no AWS region"):
        LambdaBackend(settings())


def test_an_unchanged_work_packs_the_same_bytes(scratch):
    work = scratch / "work"
    (work / "sub").mkdir(parents=True)
    (work / "sub" / "a.txt").write_text("a")
    (work / "b.bin").write_bytes(b"\0" * 100)
    first, second = scratch / "1.tar.gz", scratch / "2.tar.gz"
    digest = lambda_handler._pack(work, first)
    assert lambda_handler._pack(work, second) == digest
    assert first.read_bytes() == second.read_bytes()
    assert hashlib.md5(first.read_bytes()).hexdigest() == digest
