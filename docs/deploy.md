# Deploy

aios runs on AWS: Terraform makes the infrastructure (`infra/terraform`), and `infra/deploy.sh`
builds, migrates and rolls out each release. See the [README](../README.md) for the
architecture.

## Deploy to AWS

```sh
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # region, optional domain + ACM cert
terraform init
terraform apply                                 # VPC, RDS, Valkey, ALB, ECS, ECR, secrets

# Credentials stay out of Terraform state. Set them once:
aws secretsmanager put-secret-value --secret-id aios/external --secret-string '{
  "ANTHROPIC_API_KEY": "...", "VOYAGE_API_KEY": "..."}'

cd ../..
infra/deploy.sh          # build & push images, create roles, migrate, roll out (tag = git sha)
```

`infra/deploy.sh` builds and pushes the images, creates the database roles, migrates the
platform database, migrates the stores (`migrate-stores`: see
[Stores](stores.md#data-stores)) and rolls out.

Services won't become healthy until the first `deploy.sh` has pushed images. Everything runs
in one region (`var.region`). RDS and Valkey sit in private subnets and only accept traffic
from the app security group. Without `certificate_arn` the site is served over plain HTTP on
the ALB hostname and session cookies aren't marked `Secure`. Set a domain and certificate for
anything real.

Database credentials are in three secrets (see [Stores](stores.md#database-roles)):
`<name>/app`'s `DATABASE_URL` is `aios_app`'s, given to the api, the workers and migrations;
`<name>/provisioner` is `aios_provisioner`'s, for the `urgent-worker` and `routine-worker`
services and the `migrate-stores` task only (their execution role is the only one that can read it); `<name>/app`
also holds `DATA_STORE_CREDENTIALS_KEY`, which encrypts stores' passwords, and
`CONNECTOR_CREDENTIALS_KEY`, which encrypts connectors' credentials (the API refuses to start
with its development value when `COOKIE_SECURE` is on); and
`<name>/db-master` is the RDS master user's, for break-glass access. Only the `db-roles` task
(whose execution role is the only one that can read it) is given the master user's, to create the
roles before each migration; the secret also holds the two roles' DSNs, so that task reads nothing
else. On a stack with running services, `deploy.sh` creates the roles (a targeted apply of just
that task and secret, then the task) before the full apply switches `<name>/app` to `aios_app`, so
a task ECS starts in between never gets a role that doesn't exist yet. On a stack deployed before
these roles existed, the first deploy hands the schema to `aios_app`. Services still running the
previous release connect as the master user until they're replaced, which keeps working: the
master user is a member of `aios_app`.

## Connection pooler

The app reaches data stores through PgBouncer in transaction mode (`infra/terraform/pooler.tf`):
the `pooler` ECS service, at `pooler.<name>.internal:6432` (`DATA_STORE_POOLER_ADDRESS` in every
backend task), the runs' checkpointers among them. The platform database and the provisioner
connect directly.
RDS Proxy isn't used: it pins sessions that use advisory locks or session settings, and has no
per-database pools.

- **Pools.** One per (store database, role): `default_pool_size` server connections each (5),
  and at most `max_db_connections` per store database (10), whatever the number of app
  processes. `max_client_conn` (2,000) bounds the app's side: each process pools at most 5
  connections per store and role (1 kept, 4 while busy), and idle ones are cheap here. So the
  instance sees at most about 10 connections per active store, plus the platform's. Raise
  `max_db_connections` with the instance's `max_connections`, not with the number of tasks.
- **Connection limit.** Each store role's `CONNECTION LIMIT` (`DATA_STORE_CONNECTION_LIMIT`,
  100) is a backstop behind the pools, applied to existing stores at their next migration.
  Agents' own login roles, which only `write_data` connects as, each get
  `DATA_STORE_AGENT_CONNECTION_LIMIT` (4), and a pool of their own in the pooler.
- **No session state.** Through a transaction-mode pooler, store connections prepare no
  statements (`MAX_PREPARED_STATEMENTS=0`), pass no startup settings and take no session
  advisory locks; a run's turns take turns through a lease on the run's row
  ([Agents](agents-and-skills.md)), and stopping a turn signals it rather than ending a
  connection.
- **Authentication.** Clients log in as their own store roles; PgBouncer looks up each role's
  password with its auth query, as an auth user, on the platform database (`AUTH_DBNAME`), and
  logs in to the store with it (`<name>/pooler` holds that user's DSN, readable only by the
  pooler's execution role). In Compose and CI the auth user is the master user, a superuser,
  which can read `pg_shadow`. **Not yet deployed:** on RDS the master user can't read
  `pg_shadow`, so the auth query needs another source of the roles' secrets before the first
  apply. Until then, leave `DATA_STORE_POOLER_ADDRESS` empty on AWS, which connects directly.

## Sandboxes on Fargate (`run_python`)

On AWS, `SANDBOX_BACKEND=fargate` (`aios.sandbox.fargate`, `infra/terraform/sandbox.tf`): each
agent run that uses `run_python` (see [Tools](tools.md#run-python)) gets its own Fargate task,
so its own Firecracker microVM.

- **Starting**: the API calls `ecs:RunTask` on the sandbox task definition, passing the session's
  runner token, proxy URL and lifetimes as environment overrides, waits for the task to run and
  calls the runner (`serve` mode) at the task's private IP on port 8080. The runner exits when
  idle or at its maximum lifetime, which stops the task; the API also stops it if starting fails.
- **Network**: sandboxes run in subnets of their own whose route table has no default route.
  Their security group only admits the API (8080) and only lets them out to the egress proxy
  (3128), plus the ECR, CloudWatch Logs and S3 VPC endpoints Fargate needs to pull the image and
  ship logs. The S3 endpoint only allows ECR's layer bucket, and the interface endpoints only
  this account's credentials. RDS and Valkey don't admit sandboxes. The proxy is an ECS service
  (`egress-proxy`, API image) found at `proxy.<name>.internal` through Cloud Map.
- **IAM**: the sandbox task has no task role, so code in it has no AWS credentials. The API's
  task role (its own: the worker, web and migrate tasks share one with no permissions) may run
  only the sandbox task definition, in this cluster, and describe and stop the cluster's tasks.
- **Locally**, `docker compose` runs one `sandbox` container that every run shares
  (`SANDBOX_BACKEND=dev`). It has no isolation between runs and no network restrictions, so it's
  for development only. Allowed domains need no egress proxy there (`SANDBOX_PROXY_*` unset): the
  runner can reach every domain anyway.

Known limits:

- A sandbox takes tens of seconds to start. Starting it in the background when a turn starts
  hides most of that, but not all of it on a run's first turn.
- Each sandbox is one `ecs:RunTask` call (rate limited per account) and one IP in the sandbox
  subnets (a `/24` per AZ, so about 250 concurrent sandboxes per AZ).
- The runner token is a RunTask override, visible to anyone in the account with
  `ecs:DescribeTasks`.
- DNS lookups through the VPC resolver aren't filtered, so sandbox code can leak data in the
  names it looks up, and receive data in the answers. Route 53 DNS Firewall
  can't simply allowlist the names sandboxes need: its rule groups apply to a whole VPC, and
  the API, worker and egress proxy share this one and resolve any name (the proxy resolves
  whatever domains a run allows). Closing this means moving sandboxes into a VPC of their own
  (with their own endpoints, and the proxy and runner reached across VPCs), whose DNS Firewall
  allows only the proxy's Cloud Map name and the ECR, S3 and CloudWatch Logs endpoint names.
- The three interface endpoints cost about $45 a month across two AZs, plus data processing.

## Inbound mail (email sources)

Email sources need an inbound mail service, which the AWS stack doesn't provision. Where a
deployment has one, set `INBOUND_MAIL_DOMAIN` to the domain it receives for, and have it post
each message as JSON to `/api/inbound-mail` with `INBOUND_MAIL_SECRET` (kept as a secret, like
`CONNECTOR_CREDENTIALS_KEY`) as its bearer token, reporting its SPF and DKIM results (see
`aios.api.inbound_mail` for the shape). Without a domain, email sources are shown as
unavailable.
