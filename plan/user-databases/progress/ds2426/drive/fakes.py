"""Deterministic stand-ins for the outside world, shared by the API and the ingest worker.

- Docling: paragraphs become chunks. Voyage: a text's embedding is a bag-of-words hash.
- The chat model: searches the documents with the user's message (`search_documents`), then
  replies "Found: " and the passages it got back. Asked "which documents", it calls
  describe_data, then query_data on aios.documents, and answers from the rows.
- GitHub OAuth: /auth/callback?code=<github id> signs in that (negative) github id.
"""

import hashlib
import json
import math
import sys

sys.path.insert(0, "tests")
from fake_model import ToolCall, last_tool_results, model  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402

from aios import auth, ingest, llm, search  # noqa: E402

DIM = 1024


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for word in text.lower().split():
        v[int(hashlib.sha1(word.strip(".,!?#").encode()).hexdigest(), 16) % DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def chunk_document(filename: str, data: bytes) -> list[dict]:
    paras = [p.strip() for p in data.decode().split("\n\n") if p.strip()]
    return [{"text": p, "contextualized": p, "meta": {"headings": [filename]}} for p in paras]


async def embed_documents(texts):
    return [embed(t) for t in texts]


async def embed_query(text):
    return embed(text)


DOCS_SQL = (
    "SELECT d.filename, d.status, count(c.id) AS chunks FROM aios.documents d"
    " LEFT JOIN aios.chunks c ON c.document_id = d.id GROUP BY d.id ORDER BY d.id"
)


def _last_human(messages) -> str:
    question = next(m for m in reversed(messages) if isinstance(m, HumanMessage))
    return question.content if isinstance(question.content, str) else question.content[-1]["text"]


async def searcher(messages, info):
    print("MODEL sees tools:", sorted(info.tools), flush=True)
    ask = _last_human(messages).lower()
    if "which documents" in ask:
        # describe_data, then query_data on aios.documents, then the answer from its rows.
        results = [json.loads(m.content) for m in last_tool_results(messages)]
        if not results:
            yield ToolCall("describe_data", {})
            return
        result = results[0]
        if "error" in result:
            yield f"The data tools refused: {result['error']}"
            return
        if "tables" in result:
            names = sorted(f"{t['schema']}.{t['name']}" for t in result["tables"])
            print("MODEL described:", names, flush=True)
            yield ToolCall("query_data", {"sql": DOCS_SQL})
            return
        rows = "; ".join(f"{f} ({s}, {n} chunks)" for f, s, n in result["rows"])
        yield f"Documents in aios.documents: {rows}"
        return
    if results := last_tool_results(messages):
        found = json.loads(results[0].content)
        yield "Found: " + " | ".join(r.get("text") or r.get("error", "?") for r in found)
        if not found:
            yield "nothing"
        return
    question = next(m for m in reversed(messages) if isinstance(m, HumanMessage))
    text = question.content if isinstance(question.content, str) else question.content[-1]["text"]
    yield ToolCall("search_documents", {"query": text})


class _Resp:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


async def _token(request):
    return {"access_token": request.query_params.get("code", "-2426")}


async def _get(path, token):
    gid = int(token["access_token"])
    return _Resp({"id": gid, "login": f"ds2426-{-gid}", "name": f"DS2426 {-gid}", "email": "c@x"})


def install() -> None:
    ingest.chunk_document = chunk_document
    ingest.embed_documents = embed_documents
    search.embed_query = embed_query
    llm.chat_model = lambda name=None: model(searcher)
    auth.oauth.github.authorize_access_token = _token
    auth.oauth.github.get = _get
