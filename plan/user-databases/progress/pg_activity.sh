#!/usr/bin/env bash
# Prints each Compose container's IP, then pg_stat_activity's client connections (as the master
# user, in the db container), so each connection's role can be matched to its service.
set -euo pipefail
project="$1"
echo "## container IPs ($project)"
for c in $(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.Names}}'); do
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$c")
  echo "$c ${ip:-<stopped>}"
done
echo
echo "## pg_stat_activity (client backends)"
docker exec "${project}-db-1" psql -U aios -d aios -c \
  "SELECT usename, client_addr, state, backend_start, left(query, 60) AS last_query
     FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
     ORDER BY client_addr, backend_start"
echo "## roles"
docker exec "${project}-db-1" psql -U aios -d aios -c \
  "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_roles
    WHERE rolname IN ('aios', 'aios_app', 'aios_provisioner') ORDER BY 1"
echo "## owners"
docker exec "${project}-db-1" psql -U aios -d aios -c \
  "SELECT d.datname, pg_get_userbyid(d.datdba) AS db_owner FROM pg_database d WHERE datname = 'aios'" -c \
  "SELECT schemaname, tableowner, count(*) FROM pg_tables
    WHERE schemaname IN ('public', 'langgraph') GROUP BY 1, 2 ORDER BY 1, 2"
