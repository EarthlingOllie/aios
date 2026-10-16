"""Run the real API for a browser check, with only the outside world faked.

- Celery runs tasks eagerly (in-process), so the upload's ingest task really runs, through
  whatever the task's signature is on this checkout.
- Docling and Voyage are replaced with deterministic fakes: paragraphs become chunks, and a
  text's embedding is a bag-of-words hash.
- GitHub OAuth is faked so /auth/callback signs in a fixed user (github id -500).

Usage: python serve.py <port>
"""

import hashlib
import math
import sys

import uvicorn

from aios import auth, ingest, search
from aios.worker import celery_app

DIM = 1024


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for word in text.lower().split():
        v[int(hashlib.sha1(word.strip(".,!?").encode()).hexdigest(), 16) % DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def chunk_document(filename: str, data: bytes) -> list[dict]:
    paras = [p.strip() for p in data.decode().split("\n\n") if p.strip()]
    return [{"text": p, "contextualized": p, "meta": {"headings": [filename]}} for p in paras]


async def embed_documents(texts):
    return [embed(t) for t in texts]


async def embed_query(text):
    return embed(text)


ingest.chunk_document = chunk_document
ingest.embed_documents = embed_documents
search.embed_query = embed_query
celery_app.conf.task_always_eager = True


class _Resp:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


async def _token(request):
    return {"access_token": "fake"}


async def _get(path, token):
    return _Resp({"id": -500, "login": "browser-check", "name": "Browser Check", "email": "b@x"})


auth.oauth.github.authorize_access_token = _token
auth.oauth.github.get = _get

from aios.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
