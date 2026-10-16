"""The real API for a browser check, with only the outside world faked (`fakes.py`). Uploads are
queued through Redis to the real ingest worker (`ingest_worker.py`).

Usage: python serve.py <port>
"""

import sys

import uvicorn

sys.path.insert(0, sys.path[0])
import fakes  # noqa: E402

fakes.install()

from aios.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="info")
