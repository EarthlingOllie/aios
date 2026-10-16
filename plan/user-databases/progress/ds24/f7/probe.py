"""What psycopg exposes for a connect-time 'too many connections' FATAL."""

import psycopg

for opts in ["", "-c lc_messages=de_DE.UTF-8"]:
    try:
        psycopg.connect(
            f"host=localhost port=5497 dbname=aios_d7 user=lim password=lim options='{opts}'"
        )
    except psycopg.OperationalError as e:
        print(
            type(e).__name__,
            repr(e.sqlstate),
            repr(e.diag.sqlstate),
            repr(e.diag.severity_nonlocalized),
            str(e).strip(),
        )
