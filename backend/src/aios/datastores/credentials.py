"""Data stores' passwords: generated, kept encrypted on their rows, and given to Postgres only
as SCRAM verifiers.

A store's login roles get random passwords (`new_password`). The row keeps them encrypted
(Fernet, with a key derived from DATA_STORE_CREDENTIALS_KEY), and only `aios.datastores`
decrypts them, to connect. When the provisioner sets a role's password it sends the SCRAM
verifier (`scram_verifier`), not the password, so a server that logs statements never logs
a usable one.
"""

import base64
import hashlib
import hmac
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet

from aios.config import get_settings

# Postgres's own default (scram_iterations).
_SCRAM_ITERATIONS = 4096


def new_password() -> str:
    return secrets.token_urlsafe(32)


@lru_cache
def _fernet(key: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def _cipher() -> Fernet:
    return _fernet(get_settings().data_store_credentials_key.get_secret_value())


def encrypt(password: str) -> str:
    return _cipher().encrypt(password.encode()).decode()


def decrypt(token: str) -> str:
    return _cipher().decrypt(token.encode()).decode()


def scram_verifier(password: str) -> str:
    """The SCRAM-SHA-256 verifier Postgres stores for `password` (as `pg_authid.rolpassword`),
    which `ALTER ROLE ... PASSWORD` takes in place of the password itself (RFC 5802, 7677)."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _SCRAM_ITERATIONS)
    client_key = hmac.digest(salted, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted, b"Server Key", "sha256")
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"SCRAM-SHA-256${_SCRAM_ITERATIONS}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"
