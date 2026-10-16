import json, psycopg
pg = psycopg.connect("postgresql://aios:aios@localhost:5476/aios_q6", autocommit=True)
def try_(n, name):
    sql = "SELECT " + ", ".join(f'1 AS "{name(i)}"' for i in range(n))
    try:
        cur = pg.execute(sql)
        cols = [d.name for d in cur.description]
        print(n, "cols ok; max name bytes", max(len(c.encode()) for c in cols), "json header bytes", len(json.dumps(cols)), "one row bytes", len(json.dumps(list(cur.fetchone()))))
    except psycopg.Error as e:
        print(n, "ERROR", e.diag.message_primary)
try_(1665, lambda i: f"x{i}")
try_(1664, lambda i: f"{i:05d}" + "a" * 70)            # ascii, truncated to 63 bytes
try_(1664, lambda i: f"{i:05d}" + "é" * 40)             # 2-byte chars, json \u-escaped
try_(1664, lambda i: f"{i:05d}" + "\U0001F600" * 20)    # 4-byte chars
try_(100, lambda i: f"customer_field_{i}")              # a realistic wide table
