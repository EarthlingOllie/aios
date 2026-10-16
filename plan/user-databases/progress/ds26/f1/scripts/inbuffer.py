"""Is the memory held after a huge row libpq's input buffer (freed on close)?"""

import psycopg


def rss() -> float:
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


conn = psycopg.connect("postgresql://aios:aios@localhost:5471/aios_q1", autocommit=True)
base = rss()
cur = conn.cursor()
cur.adapters.register_loader("text", type("Skip", (psycopg.adapt.Loader,), {"load": len}))
for row in cur.stream("SELECT repeat('x', 400000000)"):
    pass
del row
print(f"after huge row, conn open: +{rss() - base:.0f}MiB")
conn.execute("SELECT 1")
print(f"after a small query on it:  +{rss() - base:.0f}MiB")
conn.close()
print(f"after closing it:           +{rss() - base:.0f}MiB")
