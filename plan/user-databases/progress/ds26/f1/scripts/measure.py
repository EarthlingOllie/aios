"""Peak memory of one `query_data` call (`_query`) returning one huge value.

usage: measure.py STORE_ID MB -> Python (tracemalloc) peak and process peak RSS growth.
"""

import json
import resource
import sys
import time
import tracemalloc
import uuid

from aios.tools.data import _query

store_id, mb = uuid.UUID(sys.argv[1]), int(sys.argv[2])
_query(store_id, "SELECT 1")  # warm up: engine, pool, imports
base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
tracemalloc.start()
t = time.monotonic()
result = _query(store_id, f"SELECT repeat('x', {mb * 1_000_000}) AS v")
took = time.monotonic() - t
_, py_peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
summary = {k: v for k, v in result.items() if k != "rows"}
summary["rows_json_bytes"] = len(json.dumps(result.get("rows")))
print(
    f"value={mb}MB python_peak={py_peak / 2**20:.0f}MiB rss_growth={rss - base:.0f}MiB"
    f" took={took:.2f}s result={summary}"
)
