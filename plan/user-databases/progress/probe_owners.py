"""Who owns the less common objects after ensure_roles, on a throwaway database."""

import uuid

import psycopg

from aios.db_roles import ensure_roles

tag = uuid.uuid4().hex[:8]
db = f"t_probe_{tag}"
base = "postgresql://aios:aios@localhost:55422"
with psycopg.connect(f"{base}/aios", autocommit=True) as c:
    c.execute(f"CREATE DATABASE {db}")
try:
    with psycopg.connect(f"{base}/{db}", autocommit=True) as c:
        c.execute("CREATE TABLE pairs (a int, b int)")
        c.execute("CREATE STATISTICS pairs_ab ON a, b FROM pairs")
        c.execute("CREATE COLLATION folded (provider = icu, locale = 'und', deterministic = false)")
        c.execute("CREATE TEXT SEARCH CONFIGURATION plain (COPY = simple)")
    ensure_roles(
        f"postgresql+psycopg://aios:aios@localhost:55422/{db}",
        f"postgresql+psycopg://t_app_{tag}:pw@localhost:55422/{db}",
        f"postgresql+psycopg://t_prov_{tag}:pw@localhost:55422/{db}",
    )
    with psycopg.connect(f"{base}/{db}", autocommit=True) as c:
        for q in [
            "SELECT 'stats', pg_get_userbyid(stxowner) FROM pg_statistic_ext WHERE stxname='pairs_ab'",
            "SELECT 'collation', pg_get_userbyid(collowner) FROM pg_collation WHERE collname='folded'",
            "SELECT 'tsconfig', pg_get_userbyid(cfgowner) FROM pg_ts_config WHERE cfgname='plain'",
        ]:
            print(c.execute(q).fetchone())
    with psycopg.connect(f"postgresql://t_app_{tag}:pw@localhost:55422/{db}", autocommit=True) as c:
        try:
            c.execute("DROP COLLATION folded")
            print("app dropped collation")
        except psycopg.Error as e:
            print("app could not drop collation:", e)
finally:
    with psycopg.connect(f"{base}/aios", autocommit=True) as c:
        c.execute(f"DROP DATABASE {db} WITH (FORCE)")
        c.execute(f"DROP ROLE t_app_{tag}")
        c.execute(f"DROP ROLE t_prov_{tag}")
