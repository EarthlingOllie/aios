#!/bin/bash
# Probe: same-role signalling, pg_stat_activity visibility, and who can revoke the signal
# functions per database. "master" stands in for the RDS master user: not a superuser.
S="psql -X -h localhost -p 5473"
export PGPASSWORD=x
psql -X -h /var/run/postgresql -p 5473 -U postgres -q <<SQL
DROP DATABASE IF EXISTS store; DROP ROLE IF EXISTS ro; DROP ROLE IF EXISTS master;
CREATE ROLE master LOGIN CREATEDB CREATEROLE PASSWORD 'x';
CREATE ROLE ro LOGIN PASSWORD 'x';
CREATE DATABASE store OWNER master;
SQL
echo "== run A: a long query as ro"
$S -U ro -d store -c "SELECT pg_sleep(20) /* run A secret */" &
sleep 1
echo "== run B (same role) sees A's query text"
$S -U ro -d store -Atc "SELECT pid, query FROM pg_stat_activity WHERE usename = current_user AND pid <> pg_backend_pid()"
echo "== run B terminates A"
$S -U ro -d store -Atc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = current_user AND pid <> pg_backend_pid()"
wait
echo "== non-superuser database owner (RDS-master-like) revokes in its own database"
$S -U master -d store -c "REVOKE EXECUTE ON FUNCTION pg_terminate_backend(int, bigint), pg_cancel_backend(int) FROM PUBLIC"
$S -U ro -d store -Atc "SELECT 'ro may still execute: ' || has_function_privilege('pg_terminate_backend(int, bigint)', 'EXECUTE')"
echo "== superuser revokes in that database"
psql -X -h /var/run/postgresql -p 5473 -U postgres -d store -c "REVOKE EXECUTE ON FUNCTION pg_terminate_backend(int, bigint), pg_cancel_backend(int) FROM PUBLIC"
$S -U ro -d store -Atc "SELECT 'ro may still execute: ' || has_function_privilege('pg_terminate_backend(int, bigint)', 'EXECUTE')"
$S -U ro -d store -Atc "SELECT pg_cancel_backend(1)"
