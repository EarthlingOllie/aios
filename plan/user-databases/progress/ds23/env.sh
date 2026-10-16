# Source me: the ds23 test environment (Postgres 16 cluster ds23 on 5443, redis on 6380).
export DATABASE_ADMIN_URL=postgresql+psycopg://aios:aios@localhost:5443/aios_ds23
export DATABASE_URL=postgresql+psycopg://aios_app:aios_app@localhost:5443/aios_ds23
export DATA_STORE_PROVISIONER_URL=postgresql+psycopg://aios_provisioner:aios_provisioner@localhost:5443/aios_ds23
export REDIS_URL=redis://localhost:6380/0
