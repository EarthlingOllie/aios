#!/bin/bash
# Drops leftover test users, store databases and store roles from ds25's cluster.
P="psql -h /var/run/postgresql -p 5525 -U postgres -v ON_ERROR_STOP=1 -q"
$P -d aios_ds25 -c "DELETE FROM users WHERE github_id < 0"
for db in $($P -Atc "select datname from pg_database where datname like 'ds\_%'"); do
  $P -c "DROP DATABASE \"$db\" WITH (FORCE)"
done
for r in $($P -Atc "select rolname from pg_roles where rolname like 'ds\_%'"); do
  $P -c "DROP ROLE \"$r\""
done
echo cleaned
