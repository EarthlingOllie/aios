"""Uploaded files, in S3 (RustFS locally): each document's at `stores/<store id>/documents/<its
id>`, under its store's prefix, and nowhere else. Postgres keeps only their metadata (the
store's `documents` rows).

Users get a file back through a download link: a presigned GetObject URL that lasts
`DOCUMENT_DOWNLOAD_URL_SECONDS` and saves the file under its original name.
"""

import hashlib
import sys
import threading
import uuid
from dataclasses import dataclass
from functools import cache
from typing import BinaryIO
from urllib.parse import quote

import boto3
import botocore.exceptions
from botocore.client import BaseClient
from botocore.config import Config

from aios.config import get_settings

# boto3's sessions aren't safe to share between threads while they make clients (the clients
# themselves are).
_clients_lock = threading.Lock()


def store_prefix(data_store_id: uuid.UUID) -> str:
    """Where everything of the store's is: deleting the store deletes what's under it."""
    return f"stores/{data_store_id}/"


def document_key(data_store_id: uuid.UUID, document_id: int) -> str:
    return f"{store_prefix(data_store_id)}documents/{document_id}"


def _new_client(endpoint: str) -> BaseClient:
    config = Config(
        signature_version="s3v4",
        # An S3-compatible server on a plain host name (rustfs:9000) has no per-bucket hosts.
        s3={"addressing_style": "path"} if endpoint else {},
    )
    settings = get_settings()
    # The keys from the settings when given (RustFS's, locally); boto3's own otherwise.
    keys = (
        {
            "aws_access_key_id": settings.data_store_files_access_key,
            "aws_secret_access_key": settings.data_store_files_secret_key.get_secret_value(),
        }
        if settings.data_store_files_access_key
        else {}
    )
    with _clients_lock:
        return boto3.session.Session().client(
            "s3", endpoint_url=endpoint or None, config=config, **keys
        )


@cache
def client() -> BaseClient:
    """The app's S3 client."""
    return _new_client(get_settings().data_store_files_endpoint)


@cache
def _signer() -> BaseClient:
    """Signs download links, for the endpoint the browser reaches."""
    settings = get_settings()
    public = settings.data_store_files_public_endpoint
    return _new_client(public) if public else client()


@dataclass(frozen=True)
class Stored:
    size_bytes: int
    sha256: str


class _Measured:
    """Reads through to the file, counting and hashing what's read. It has no `seek`, so the
    upload reads it once, in order."""

    def __init__(self, file: BinaryIO):
        self._file = file
        self.size = 0
        self.sha256 = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self._file.read(size)
        self.size += len(data)
        self.sha256.update(data)
        return data


def put(key: str, file: BinaryIO, content_type: str | None) -> Stored:
    """Streams the file to S3 at `key` (in parts, if it's large)."""
    measured = _Measured(file)
    client().upload_fileobj(
        measured,
        get_settings().data_store_files_bucket,
        key,
        ExtraArgs={"ContentType": content_type or "application/octet-stream"},
    )
    return Stored(size_bytes=measured.size, sha256=measured.sha256.hexdigest())


class FilesUnavailable(RuntimeError):
    """S3 couldn't be reached, or asked to slow down, or failed on its side (after the client's
    own retries): worth trying again later. The original error is the cause."""


# S3 (or KMS, behind it) refusing for now rather than for good.
_TRANSIENT_CODES = {"SlowDown", "RequestTimeout", "Throttling", "ThrottlingException"}


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (botocore.exceptions.ConnectionError, botocore.exceptions.HTTPClientError)):
        return True
    if isinstance(exc, botocore.exceptions.ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return status >= 500 or exc.response.get("Error", {}).get("Code") in _TRANSIENT_CODES
    return False


def read(key: str) -> bytes:
    """The object's bytes. Raises `FilesUnavailable` when S3 can't answer for now."""
    try:
        return _read(key)
    except Exception as exc:
        if _is_transient(exc):
            raise FilesUnavailable(f"Couldn't read {key} from S3 for now") from exc
        raise


def _read(key: str) -> bytes:
    return (
        client().get_object(Bucket=get_settings().data_store_files_bucket, Key=key)["Body"].read()
    )


def delete(key: str) -> None:
    """Deletes the object; one already gone is no error. The bucket keeps no versions, so it's
    gone for good."""
    client().delete_object(Bucket=get_settings().data_store_files_bucket, Key=key)


# The most objects S3 deletes in one request.
DELETE_BATCH = 1000


def delete_under(prefix: str) -> None:
    """Deletes every object whose key starts with `prefix`, a page at a time. Run again after
    it's cut short, it deletes the rest."""
    s3, bucket = client(), get_settings().data_store_files_bucket
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix, PaginationConfig={"PageSize": DELETE_BATCH}
    ):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if not keys:
            continue
        result = s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
        if errors := result.get("Errors"):
            raise RuntimeError(f"S3 didn't delete {len(errors)} under {prefix}: {errors[0]}")


def attachment(filename: str) -> str:
    """A Content-Disposition that saves the file under `filename`: in full (RFC 6266's
    `filename*`), with an ASCII stand-in for browsers that don't read that."""
    filename = filename or "download"
    ascii_name = "".join(c if " " <= c <= "~" and c not in '"\\' else "_" for c in filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def download_url(key: str, filename: str) -> str:
    """A link that downloads the object as `filename` until it expires: a bearer link, so mint
    one per download and don't show it."""
    return _signer().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": get_settings().data_store_files_bucket,
            "Key": key,
            "ResponseContentDisposition": attachment(filename),
        },
        ExpiresIn=get_settings().document_download_url_seconds,
    )


def create_bucket() -> None:
    """Creates the bucket if it's missing: for RustFS, locally (Terraform makes it on AWS)."""
    s3 = client()
    bucket = get_settings().data_store_files_bucket
    try:
        s3.head_bucket(Bucket=bucket)
    except s3.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
        s3.create_bucket(Bucket=bucket)


def main(argv: list[str]) -> int:
    if argv != ["create-bucket"]:
        print("usage: python -m aios.datastores.files create-bucket", file=sys.stderr)
        return 2
    create_bucket()
    print(f"Bucket {get_settings().data_store_files_bucket} is ready")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
