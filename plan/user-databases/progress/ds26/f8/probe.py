import sys

import psycopg

sys.path.insert(0, "src")
from aios.tools import data  # noqa: E402

pg = psycopg.connect("host=localhost port=5478 dbname=aios_q8 user=aios password=aios", autocommit=True)
pg.prepare_threshold = None
cases = {
    "deep jsonb": "SELECT (repeat('[', 5000) || repeat(']', 5000))::jsonb",
    "deep json": "SELECT (repeat('[', 5000) || repeat(']', 5000))::json",
    "deep jsonb 900": "SELECT (repeat('[', 900) || repeat(']', 900))::jsonb",
    "NUL in sql": "SELECT 1 -- \x00",
    "lone surrogate": "SELECT '\ud800'",
    "infinity date": "SELECT 'infinity'::date",
    "big interval": "SELECT interval '200000000 years'",
}
for k, sql in cases.items():
    try:
        pg.execute("BEGIN READ ONLY")
        r = data._read(pg, sql)
        print(k, "OK", str(r)[:100])
    except psycopg.Error as e:
        print(k, "psycopg.Error", type(e).__name__, str(e)[:100])
    except BaseException as e:
        print(k, "UNCAUGHT", type(e).__name__, str(e)[:100])
    finally:
        try:
            pg.execute("ROLLBACK")
        except Exception as e:
            print("  rollback failed", e)
