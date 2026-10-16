"""Prototype: per-cursor loaders that refuse an oversized value before decoding it."""

import psycopg
from psycopg import adapt, pq
INVALID_OID = 0


class TooBig(Exception):
    pass


def capped(cur, oids, max_bytes):
    for oid in set(oids):
        inner = cur.adapters.get_loader(oid, pq.Format.TEXT) or cur.adapters.get_loader(
            INVALID_OID, pq.Format.TEXT
        )

        class Capped(adapt.Loader):
            _inner_cls = inner

            def __init__(self, oid, context=None):
                super().__init__(oid, context)
                self._inner = self._inner_cls(oid, context)

            def load(self, data):
                if len(data) > max_bytes:
                    raise TooBig
                return self._inner.load(data)

        cur.adapters.register_loader(oid, Capped)


conn = psycopg.connect("postgresql://aios:aios@localhost:5471/aios_q1", autocommit=True)
sql = (
    "SELECT 1.10::numeric AS n, 'NaN'::float8, date '2026-10-16', '\\x0102'::bytea,"
    " '{\"a\": [1]}'::jsonb, ARRAY[1, 2], ARRAY['a', 'b'], true, NULL, 'x'::text"
    " UNION ALL SELECT 2, 1, null, null, null, null, null, false, null, repeat('y', 5000)"
)
cur = conn.cursor()
res = conn.pgconn.prepare(b"", sql.encode())
d = conn.pgconn.describe_prepared(b"")
oids = [d.ftype(i) for i in range(d.nfields)]
print(oids)
capped(cur, oids, 1000)
try:
    for row in cur.stream(sql):
        print(row)
except TooBig:
    print("too big; status", conn.pgconn.transaction_status)
print(conn.execute("SELECT 42").fetchone())
