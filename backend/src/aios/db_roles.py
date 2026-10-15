"""The platform database's roles, created by the master user: `python -m aios.db_roles`.

- The app's role (`aios_app`, DATABASE_URL's user): what the API, the workers and migrations
  connect as. It owns the platform schema (`public`, and everything in the database), may
  create schemas, and has no `CREATEDB` or `CREATEROLE`. The database stays the master user's, so
  the app's role can't drop or reconfigure it.
- The provisioner's role (`aios_provisioner`, DATA_STORE_PROVISIONER_URL's user): `CREATEDB` and
  `CREATEROLE`, for creating data stores. Only the provisioning worker has its credentials.

The names and passwords come from those two URLs, and the master user's from
DATABASE_ADMIN_URL, which nothing else is given: past this one-off, the master user is for
break-glass access only. Re-running is safe: it updates the passwords (so rotating one is
changing its URL and running this again), and hands the app's role anything in the platform
database the master user still owns, which is everything on a database that was migrated as
the master user.
"""

import sys

import psycopg
from psycopg import sql
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from aios.config import Settings


class _BootstrapSettings(Settings):
    """The app's settings, plus the master user's DSN, which only this command reads."""

    database_admin_url: str


def _conninfo(url: str, **parts) -> str:
    """A SQLAlchemy URL as plain libpq connection info."""
    return make_url(url).set(drivername="postgresql", **parts).render_as_string(hide_password=False)


def _login_role(conn: psycopg.Connection, url: str, *, privileged: bool) -> str:
    """Create or update the login role named by `url`, with its password; returns its name."""
    u = make_url(url)
    name = sql.Identifier(u.username)
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (u.username,)).fetchone()
    if not exists:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(name))
    attrs = "CREATEDB CREATEROLE" if privileged else "NOCREATEDB NOCREATEROLE"
    conn.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN {} PASSWORD {}").format(
            name, sql.SQL(attrs), sql.Literal(u.password)
        )
    )
    return u.username


# What the current user owns in the database's own schemas, as (the ALTER statement's object
# kind, the object's name as SQL). Only objects whose owner can be changed on their own: a
# table's indexes, row type, TOAST table and column sequences, and a type's array type, follow
# it. Extensions' members (pgvector's) are left alone.
_OWNED = """
WITH me AS (
    -- Not current_user::regrole, which reads the name as an unquoted identifier.
    SELECT oid FROM pg_roles WHERE rolname = current_user
),
own AS (
    SELECT n.oid AS nsp FROM pg_namespace n
     WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
)
SELECT kind, name FROM (
    SELECT 'SCHEMA' AS kind, quote_ident(nspname) AS name,
           'pg_namespace'::regclass AS cls, oid
      FROM pg_namespace
     WHERE oid IN (SELECT nsp FROM own) AND nspowner = (SELECT oid FROM me)
    UNION ALL
    SELECT CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                          WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE'
                          ELSE 'TABLE' END,
           c.oid::regclass::text, 'pg_class'::regclass, c.oid
      FROM pg_class c
     WHERE c.relnamespace IN (SELECT nsp FROM own) AND c.relowner = (SELECT oid FROM me)
       AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
       -- A column's sequence (serial or identity) follows its table.
       AND NOT EXISTS (SELECT 1 FROM pg_depend d
                        WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                          AND d.deptype IN ('a', 'i'))
    UNION ALL
    SELECT CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,
           t.oid::regtype::text, 'pg_type'::regclass, t.oid
      FROM pg_type t
     WHERE t.typnamespace IN (SELECT nsp FROM own) AND t.typowner = (SELECT oid FROM me)
       -- Enums, domains, ranges and standalone composite types; not tables' row types, array
       -- types or multiranges, which follow their table or type.
       AND (t.typtype IN ('e', 'd', 'r')
            OR (t.typtype = 'c'
                AND (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c'))
    UNION ALL
    SELECT 'ROUTINE', p.oid::regprocedure::text, 'pg_proc'::regclass, p.oid
      FROM pg_proc p
     WHERE p.pronamespace IN (SELECT nsp FROM own) AND p.proowner = (SELECT oid FROM me)
) o
WHERE NOT EXISTS (SELECT 1 FROM pg_depend d
                   WHERE d.classid = o.cls AND d.objid = o.oid AND d.deptype = 'e')
"""


def ensure_roles(admin_url: str, app_url: str, provisioner_url: str) -> None:
    """Create or update both roles, and make the app's role own the platform schema.

    The platform database is `app_url`'s; the master user connects to it whatever database
    `admin_url` names.
    """
    admin, app_u, prov_u = make_url(admin_url), make_url(app_url), make_url(provisioner_url)
    # Before anything changes: the same role twice would end with the last ALTER ROLE's
    # attributes and password (an app role with CREATEDB, or a master user without it), and a
    # URL without a password would clear the role's.
    names = [admin.username, app_u.username, prov_u.username]
    if len(set(names)) != 3:
        raise ValueError(
            "The master user's, the app's and the provisioner's URLs must name three different "
            f"roles, not {names}"
        )
    for u in (app_u, prov_u):
        if not u.password:
            raise ValueError(f"The URL for role {u.username} has no password")
    dbname = app_u.database
    if not dbname:
        raise ValueError("The app's URL names no database")
    with psycopg.connect(_conninfo(admin_url, database=dbname)) as conn:
        app = sql.Identifier(_login_role(conn, app_url, privileged=False))
        _login_role(conn, provisioner_url, privileged=True)
        # Handing objects to a role takes membership of it, and on RDS the master user isn't a
        # superuser. It also keeps the master user able to use them, for break-glass access, and
        # so services still running as it during a deploy's switchover keep working.
        conn.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(app))
        # pgvector isn't a trusted extension, so the app's role can't create it (migration 0001
        # only creates it if it's missing).
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # The app's role owns the platform schema but not the database, which would let it drop
        # or reconfigure it. CREATE on the database is for the checkpointer's `langgraph` schema.
        conn.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(sql.Identifier(dbname), app)
        )
        conn.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(app))
        # What was created as the master user (the whole schema, on a database migrated before
        # this role existed), so the app's role can migrate it.
        for kind, name in conn.execute(_OWNED).fetchall():
            conn.execute(
                sql.SQL("ALTER {} {} OWNER TO {}").format(sql.SQL(kind), sql.SQL(name), app)
            )


def main() -> None:
    try:
        settings = _BootstrapSettings()
    except ValidationError:
        sys.exit("DATABASE_ADMIN_URL (the master user's DSN) is required")
    ensure_roles(
        settings.database_admin_url, settings.database_url, settings.data_store_provisioner_url
    )
    print(
        f"Roles {make_url(settings.database_url).username} and "
        f"{make_url(settings.data_store_provisioner_url).username} are ready"
    )


if __name__ == "__main__":
    main()
