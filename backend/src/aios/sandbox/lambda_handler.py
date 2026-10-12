"""The sandbox as a Lambda function, invoked once per `run_python` call (`aws_lambda`).

This file is copied into the `sandbox-lambda` image next to `runner.py` (see the Dockerfile), so it
uses the standard library only and never imports `aios`. The image runs it with Lambda's runtime
interface client: `python -m awslambdaric lambda_handler.handler`.

An invocation's event is one of:

    {"ping": true}    starts an execution environment ahead of the session's first call
    {"session", "code", "timeout", "proxy_url", "work_limit", "restore", "save"}

A session's /work lives in S3 between calls, as one gzipped tar that the API reaches through the
presigned URLs in the event: `restore` (a GET) and `save` (a POST, whose policy caps its size).
The function has no S3 access of its own. Each call:

1. restores /work, unless this environment already holds the session's latest copy (a conditional
   GET on the archive's MD5, which is its S3 ETag)
2. runs the code with the runner, as on the other backends
3. saves /work if it changed, unless it's over `work_limit` unpacked. A call that fails, or
   whose /work can't be saved, leaves the session's /work as it was before the call.

Lambda's filesystem is read-only apart from /tmp, so the image sets SANDBOX_WORKDIR=/tmp/work and
links /work to it. Each session is invoked as its own Lambda tenant, so an environment only ever
serves one session; that's what keeps sessions apart (the code runs as the handler's user, and
could, say, take the next invocation from the local runtime API). As a backstop, an environment
that last served another session empties /tmp before it restores this one's /work.
"""

import contextlib
import gzip
import hashlib
import io
import os
import secrets
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

try:
    from aios.sandbox import runner
except ImportError:  # In the sandbox image, where `aios` isn't installed.
    import runner  # type: ignore[no-redef]

TRANSFER_TIMEOUT = 60
CHUNK = 1024 * 1024
# Characters that would break out of a multipart form field's header or value.
_UNSAFE_FIELD = set('"\r\n')

# Never through a proxy: S3 is reached over the VPC's gateway endpoint, and the egress proxy is
# only for the code's own requests.
_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Failed(Exception):
    """The call can't go ahead; the message is shown to the model."""


class _State:
    # The session this environment last served, and the MD5 of the archive its /work matches
    # (None: unknown, so the next call restores /work from S3 in full).
    session: str | None = None
    digest: str | None = None


_state = _State()


def handler(event: dict, context=None) -> dict:
    if event.get("ping"):
        return {"ok": True}
    try:
        session = str(event["session"])
        code, timeout = str(event["code"]), float(event["timeout"])
        proxy_url, limit = str(event.get("proxy_url") or ""), int(event["work_limit"])
        restore, save = str(event["restore"]), event["save"]
    except (KeyError, TypeError, ValueError):
        return {"error": "The sandbox got a malformed request."}
    try:
        _restore(session, restore, limit)
    except Failed as exc:
        _state.digest = None
        return {"error": str(exc)}

    if proxy_url:
        os.environ["SANDBOX_PROXY_URL"] = proxy_url
    else:
        os.environ.pop("SANDBOX_PROXY_URL", None)
    runner.prepare_workdir()
    try:
        result = runner.execute(code, timeout)
    except BaseException:
        _state.digest = None  # /work may have changed without being saved.
        raise

    if note := _save(save, limit):
        # Out of the model's way at the end, past the runner's own cap.
        result["stderr"] += f"\n[sandbox] {note}\n"
    return {"result": result}


# --- restore ---


def _scratch() -> Path:
    """/tmp in the image: everything the code could have written outside /work."""
    return Path(tempfile.gettempdir())


def _empty(directory: Path) -> None:
    for entry in directory.iterdir() if directory.is_dir() else []:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()


def _fresh(work: Path) -> None:
    """Makes /work an empty directory, whatever the code left there (even a link elsewhere, whose
    target is left alone)."""
    if work.is_symlink() or (work.exists() and not work.is_dir()):
        work.unlink()
    elif work.is_dir():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)


def _restore(session: str, url: str, limit: int) -> None:
    work = Path(runner.WORKDIR)
    if _state.session != session:
        # Another session's files, whether in /work or elsewhere in /tmp.
        _empty(_scratch())
        _state.session, _state.digest = session, None
    headers = {}
    if _state.digest and work.is_dir() and not work.is_symlink():
        headers["If-None-Match"] = f'"{_state.digest}"'
    try:
        resp = _http.open(urllib.request.Request(url, headers=headers), timeout=TRANSFER_TIMEOUT)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 304:
            return  # This environment's /work is the session's latest.
        if exc.code == 404:
            # Nothing saved yet: the session's first call (or it was stopped).
            _fresh(work)
            _state.digest = None
            return
        raise Failed(f"Couldn't restore /work ({exc.code}), so the code wasn't run.") from None
    except OSError as exc:
        raise Failed(
            f"Couldn't restore /work ({type(exc).__name__}), so the code wasn't run."
        ) from None

    fd, name = tempfile.mkstemp(prefix=".work-", suffix=".tar.gz", dir=_scratch())
    archive = Path(name)
    try:
        digest = hashlib.md5(usedforsecurity=False)
        with resp, os.fdopen(fd, "wb") as f:
            while chunk := resp.read(CHUNK):
                digest.update(chunk)
                f.write(chunk)
        _fresh(work)
        _unpack(archive, work, limit)
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise Failed(
            f"Couldn't restore /work ({type(exc).__name__}), so the code wasn't run."
        ) from None
    finally:
        archive.unlink(missing_ok=True)
    _state.digest = digest.hexdigest()


def _keep(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
    """Extracts what `tarfile`'s `data` filter allows (no paths or links out of /work, no device
    files) and skips the rest, rather than failing the whole restore over one entry."""
    try:
        return tarfile.data_filter(member, path)
    except tarfile.FilterError:
        return None


def _unpack(archive: Path, work: Path, limit: int) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if sum(m.size for m in members if m.isfile()) > limit:
            raise Failed("The saved /work is over the size limit, so the code wasn't run.")
        tar.extractall(work, members=members, filter=_keep)
    if os.geteuid() == 0:
        # Run as root (local development), the runner runs the code as another user, which must
        # own its files. On Lambda, the function's own user runs both.
        for path in [work, *work.rglob("*")]:
            os.lchown(path, runner.CHILD_UID, runner.CHILD_UID)


# --- save ---


def _size(work: Path) -> int:
    total = 0
    for path in work.rglob("*"):
        with contextlib.suppress(OSError):
            if path.is_file() and not path.is_symlink():
                total += path.lstat().st_size
    return total


def _portable(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    # Files, directories and links only: no FIFOs or device files, which `_keep` would skip.
    if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _pack(work: Path, archive: Path) -> str:
    """Writes /work to `archive` and returns its MD5. The same /work always packs the same bytes
    (no timestamp or name in the gzip header, entries in name order), so an unchanged /work is
    noticed."""
    digest = hashlib.md5(usedforsecurity=False)
    with (
        open(archive, "wb") as f,
        gzip.GzipFile(fileobj=_Hashing(f, digest), mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        for entry in sorted(work.iterdir()):
            tar.add(entry, arcname=entry.name, filter=_portable)
    return digest.hexdigest()


class _Hashing:
    """A write-only file that hashes what passes through it."""

    def __init__(self, f, digest) -> None:
        self.f, self.digest = f, digest

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        return self.f.write(data)

    def flush(self) -> None:
        self.f.flush()


def _save(save: dict, limit: int) -> str | None:
    """Saves /work. Returns a note for the model if it couldn't be."""
    work = Path(runner.WORKDIR)
    if work.is_symlink() or not work.is_dir():
        # The code removed /work, or replaced it: what's saved is an empty one.
        _fresh(work)
    if (size := _size(work)) > limit:
        _state.digest = None
        return (
            f"/work holds {size / 2**20:.0f} MB, over the {limit / 2**20:.0f} MB limit, so this"
            " call's changes to it weren't kept. The next call starts from /work as it was"
            " before this call."
        )
    fd, name = tempfile.mkstemp(prefix=".work-", suffix=".tar.gz", dir=_scratch())
    os.close(fd)
    archive = Path(name)
    try:
        digest = _pack(work, archive)
        if digest == _state.digest:
            return None  # Unchanged.
        _post(save, archive)
    except (OSError, tarfile.TarError, ValueError) as exc:
        _state.digest = None
        return (
            f"/work couldn't be saved ({type(exc).__name__}), so this call's changes to it"
            " weren't kept. The next call starts from /work as it was before this call."
        )
    finally:
        archive.unlink(missing_ok=True)
    _state.digest = digest
    return None


def _post(save: dict, archive: Path) -> None:
    """Uploads `archive` with S3's presigned POST (`save` is `{"url", "fields"}`)."""
    url, fields = str(save["url"]), {str(k): str(v) for k, v in save["fields"].items()}
    if any(c in _UNSAFE_FIELD for item in fields.items() for c in "".join(item)):
        raise ValueError("unexpected characters in the upload form")
    boundary = secrets.token_hex(16)
    head = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items()
    )
    head += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file";'
        ' filename="work.tar.gz"\r\nContent-Type: application/gzip\r\n\r\n'
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    size = archive.stat().st_size
    with open(archive, "rb") as f:
        body = _Chain(io.BytesIO(head), f, io.BytesIO(tail))
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(head) + size + len(tail)),
            },
        )
        with _http.open(request, timeout=TRANSFER_TIMEOUT) as resp:
            resp.read()


class _Chain:
    """Reads several files one after the other, so the upload streams from disk."""

    def __init__(self, *parts) -> None:
        self.parts = list(parts)

    def read(self, size: int = -1) -> bytes:
        while self.parts:
            data = self.parts[0].read(size)
            if data:
                return data
            self.parts.pop(0)
        return b""
