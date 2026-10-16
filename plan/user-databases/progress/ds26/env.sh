#!/bin/bash
# Usage: env.sh <command...>  (runs it with this builder's environment)
export DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:5446/aios_ds26
export DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:5446/aios_ds26
export DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:5446/aios_ds26
export REDIS_URL=redis://localhost:6386/0
exec "$@"
