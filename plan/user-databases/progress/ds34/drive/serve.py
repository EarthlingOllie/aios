"""Run the real API for the ds34 browser check, with only the outside world faked.

- Provisioning, the history set-up and the history export go through the broker (Redis) to the
  real workers (the provisioning worker, and an app worker on the default queue).
- The chat model is a scripted fake (tests/fake_model.py): it answers "echo: <message>".
- GitHub OAuth is faked so /auth/callback signs in a fixed user (github id -3400).

Usage: python serve.py <port>   (run from backend/)
"""

import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path.cwd() / "tests"))

from fake_model import model  # noqa: E402

from aios import auth, llm  # noqa: E402


async def echo(messages, info):
    yield f"echo: {messages[-1].text}"


llm.chat_model = lambda name=None: model(echo)


class _Resp:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


async def _token(request):
    return {"access_token": "fake"}


async def _get(path, token):
    return _Resp({"id": -3400, "login": "ds34-check", "name": "DS34 Check", "email": "c@x"})


auth.oauth.github.authorize_access_token = _token
auth.oauth.github.get = _get

from aios.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="info")
