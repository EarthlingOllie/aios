"""Password hashing: Argon2id through pwdlib.

Hashing and verifying take tens of milliseconds of CPU, so call these from sync endpoints
(FastAPI runs them in the threadpool), never on the event loop.
"""

from functools import cache

from pwdlib import PasswordHash

_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    """`password`'s Argon2id PHC string, with a fresh salt."""
    return _hasher.hash(password)


@cache
def _dummy_hash() -> str:
    return _hasher.hash("not anyone's password")


def verify_password(password: str, password_hash: str | None) -> tuple[bool, str | None]:
    """Whether `password` matches `password_hash`, and a new hash for it when the stored one was
    made with other parameters than today's (save it), else None.

    With no hash (no such user), it still verifies against a dummy one, so the answer takes as
    long either way and its timing doesn't tell whether the account exists. It's always False.
    """
    if password_hash is None:
        _hasher.verify(password, _dummy_hash())
        return False, None
    return _hasher.verify_and_update(password, password_hash)
