"""Current RSS (not peak) after a huge-value `query_data` call and after the next small one:
does memory stay held (libpq's input buffer on the pooled connection)?"""

import sys
import uuid

from aios.tools.data import _query


def rss() -> float:
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


store_id, mb = uuid.UUID(sys.argv[1]), int(sys.argv[2])
_query(store_id, "SELECT 1")
base = rss()
_query(store_id, f"SELECT repeat('x', {mb * 1_000_000}) AS v")
after = rss()
_query(store_id, "SELECT 1")
after_next = rss()
print(
    f"value={mb}MB rss_now_after_call=+{after - base:.0f}MiB"
    f" rss_now_after_next_small_call=+{after_next - base:.0f}MiB"
)
