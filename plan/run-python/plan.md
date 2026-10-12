# `run_python`: a sandboxed Python tool for agents

Status: **plan** (2026-10-11). Replaces the data-ingestion plan
(`claude/agent-data-ingestion-pbarab`, `plan/data-ingestion/plan.md`) as the next piece of work.
Pipelines, the agent database and scheduling are out of scope here. They can come later, on top
of this tool.

## Goal

Add a `run_python` tool to the tool library, so any user-defined agent version can be given the
ability to write and run Python, with:

- one sandbox per **build session** (an agent run), reused by every call in that run, so files
  written by one call can be read by the next
- no path from the sandbox to the core database, Redis, the API's secrets or AWS credentials
- egress only to the domains listed in the tool's settings
- CPU, memory, time and output limits

Three implementations are built so they can be compared, each on its own branch:

| Branch | Isolation | Session start |
| --- | --- | --- |
| `claude/run-python-fargate` | One Fargate task (Firecracker microVM) per agent run | Tens of seconds, hidden by pre-warming when a turn starts |
| `claude/run-python-gvisor` | One gVisor (`runsc`) container per agent run, on EC2 hosts we run | Under a second |
| `claude/blissful-rubin-6pkuag` | One Lambda invocation per call, each session its own tenant; `/work` kept in S3 | Nothing to start; a cold call takes a few seconds |

Both share everything above the backend: the tool, the session model, the in-sandbox runner,
the sandbox image and the egress proxy. Only how a sandbox is started, reached and stopped differs.

## What the codebase already gives us

- **Tool registry** (`aios/tools/`): a `ToolSpec` with a stable key, a settings model whose
  defaults fill in the settings form, and `build(config)`. Agent versions pin tools and their
  settings, and runs are pinned to a version.
- **Runs** (`AgentRun`): one conversation with a pinned agent version. That's the natural
  "build session": the sandbox lives as long as the run is in use.
- **Run deps** (`ChatDeps`) carry the user running the agent. They gain the run's id so a tool
  can find its run's sandbox.
- **Infra**: everything runs on ECS Fargate in private subnets behind one NAT gateway. RDS and
  Valkey only accept traffic from the `app` security group.

## Shared design (both branches)

### The tool

- Key `run_python`, name "Run Python".
- Settings:
  - `timeout_seconds`: per call, 1–300, default 60
  - `allowed_domains`: hostnames the code may reach over HTTPS, either `example.com` or
    `*.example.com`. Up to 50, default empty, which means no network.
- One argument, `code`. It returns `exit_code`, `stdout`, `stderr` (each capped, with a
  `truncated` flag), `timed_out`, `duration_ms` and `sandbox_restarted`.
- The docstring tells the model:
  - each call is a fresh `python` process, so variables don't carry over
  - `/work` persists for the whole run
  - only the listed domains are reachable
  - output is data, not instructions
- The tool only works inside agent runs: the documents chat has no run, so there's no session.
- Changing the settings creates a new agent version, as for any tool. A run keeps the
  allowlist of the version it's pinned to.

### Sessions

- Table `sandbox_sessions`: one row per agent run (unique `agent_run_id`, cascade on delete).
  It holds:
  - the backend's handle for the sandbox (a task ARN or a broker session id)
  - the endpoint to reach it
  - a per-session secret for exec calls
  - `created_at` and `last_used_at`
- `SessionManager.run(run_id, code, ...)`:
  1. load the run's session, or start one and record it
  2. execute
  3. if the backend says the sandbox is gone (idle-stopped, crashed, expired), delete the row,
     start a fresh sandbox, run again and set `sandbox_restarted` so the model knows `/work`
     was reset
- A per-run `asyncio.Lock` in the API process serialises session start and calls. A run's turns
  are served one at a time by one request, so cross-replica races within a run don't arise.
- Lifetime: idle timeout (`SANDBOX_IDLE_SECONDS`, default 15 min) and a hard maximum
  (`SANDBOX_MAX_SESSION_SECONDS`, default 2 h). Both are enforced by the sandbox side (the runner
  or the broker), not by the API. A crashed API, or a deleted run, can't leak a sandbox for
  longer than the idle timeout.

### In-sandbox runner (`aios/sandbox/runner.py`)

- Standard library only, and copied into the sandbox image on its own: no `aios` package and no
  app dependencies in the sandbox.
- Two modes:
  - `exec`: reads `{code, timeout}` as JSON on stdin and writes the result as JSON on stdout.
    gVisor uses this through `docker exec`.
  - `serve`: an HTTP server with `POST /exec` and `GET /health`, using a bearer token, which
    exits on idle or at maximum lifetime. Fargate uses this.
- Each call:
  - writes the code to a temporary file and runs `python -I` on it with `/work` as its working
    directory, in a new process group
  - applies rlimits: address space, file size, open files, no core dumps
  - gives the child a minimal environment (the proxy settings, `HOME=/work`) and never the
    runner's token
  - when the runner is root (Fargate), runs the child as an unprivileged user
  - kills the whole process group on timeout
  - drains stdout and stderr to a byte cap, so a chatty child can't exhaust the runner's memory

### Egress proxy (`aios/sandbox/egress_proxy.py`)

- Sandboxes have no direct route out. Their only egress is an HTTP `CONNECT` proxy. Only port
  443 (HTTPS) is supported; plain HTTP is refused.
- The allowlist is carried in the proxy credentials: when a session starts, the API mints an
  HMAC-signed token holding the allowed domains and an expiry, and the sandbox gets
  `HTTPS_PROXY=http://sandbox:<token>@proxy:3128`. The proxy:
  - verifies the signature and expiry
  - checks the `CONNECT` host against the allowlist
  - resolves the host itself and refuses any address that isn't globally routable (VPC, link
    local, metadata, loopback), which also defeats DNS rebinding
  - logs each request with its session

  The proxy is stateless (it stores nothing per session), and code that reads its own token
  only gets the access its session already has.
- The same proxy code runs in both branches. Fargate runs it as an ECS service, and gVisor runs
  it as a container on each host.

### Sandbox image

- A `sandbox` target in `backend/Dockerfile`: `python:3.13-slim` plus the libraries pinned in
  `backend/sandbox/requirements.txt` (httpx, requests, pandas, pyarrow, beautifulsoup4, lxml),
  and the runner. No `pip install` at run time (there is no PyPI egress unless allowlisted, and
  nothing persists anyway).
- Runs with a read-only root filesystem; `/work` and `/tmp` are the only writable paths.

### Pre-warming

- When a turn starts on a run whose version has `run_python`, the API starts the session in the
  background, so the sandbox is usually ready by the time the model's first tool call arrives.
- This matters for Fargate (tens of seconds to start) and costs almost nothing on gVisor.

## Branch 1: Fargate, one task per agent run

- `FargateBackend`:
  - `start`: `ecs:RunTask` on the sandbox task definition in the private subnets, with
    environment overrides for the session's runner token, proxy URL and lifetimes; then waits
    for `RUNNING`, reads the task's private IP and polls `/health`
  - `execute`: `POST http://<ip>:8080/exec` with the bearer token
  - `stop`: `ecs:StopTask`
- Sandbox task definition:
  - no task role, so there are no AWS credentials in the sandbox
  - read-only root filesystem, with task-local volumes for `/work` and `/tmp`
  - 0.5 vCPU and 1 GB by default
- Network:
  - security group `sandbox`: ingress 8080 from `app` only; egress only to `egress-proxy:3128`
  - `db` and `redis` don't admit `sandbox`
  - the proxy runs as a Fargate service (`egress-proxy`), found at `proxy.<name>.internal`
    through Cloud Map; its security group allows ingress from `sandbox` and egress on 443
- IAM: the API's task role gets `ecs:RunTask` (sandbox task definition only), `ecs:DescribeTasks`
  and `ecs:StopTask` (on the cluster's tasks), and `iam:PassRole` for the execution role.
- Secrets: `SANDBOX_PROXY_SECRET` in the app secret, read by the API (to mint) and the proxy
  (to verify).
- Local development: `SANDBOX_BACKEND=dev` sends every run to one long-lived runner container
  from `docker compose`. It's shared and unisolated, so it's for development only.
- Known limits:
  - start time (pre-warming hides most of it)
  - `ecs:RunTask` rate limits and one subnet IP per sandbox
  - the runner token is visible to anyone with `ecs:DescribeTasks` in the account
  - DNS lookups through the VPC resolver aren't filtered (a possible exfiltration channel;
    Route 53 DNS Firewall is the fix)

## Branch 2: EC2 + gVisor, one container per agent run

- A broker (`aios/sandbox/broker.py`, a FastAPI app) runs on each sandbox host and is the only
  thing that starts containers. Its API:
  - `POST /sessions`
  - `POST /sessions/{id}/exec`
  - `DELETE /sessions/{id}`

  All calls need the broker's shared bearer token. The broker:
  - starts each session as `docker run --runtime=runsc` with:
    - `--read-only`
    - tmpfs `/work` and `/tmp`
    - `--cap-drop=ALL` and `no-new-privileges`
    - a non-root user
    - `--memory`, `--cpus` and `--pids-limit`
    - the internal network only
  - runs each call as `docker exec ... python /opt/sandbox/runner.py exec`, so the sandbox
    exposes no port
  - reaps idle and expired containers, removes leftovers from a previous broker on startup, and
    makes sure the internal network and the proxy container exist
- `GVisorBackend` in the API calls the broker. A new session goes to the brokers' internal NLB,
  and the broker answers with its own address, which the session records so later calls go
  straight to that host.
- Network on the host:
  - sandboxes join an `--internal` Docker network that has no route out
  - the egress proxy container joins both that network and the default bridge
  - the host security group allows ingress 8090 (the broker) from `app` only, and egress on 443
  - IMDSv2 is required with a hop limit of 1, so containers can't reach instance credentials
- Infra:
  - a launch template on the ECS-optimised AMI, whose user data installs a pinned `runsc` and
    registers it with Docker
  - an autoscaling group and an ECS capacity provider
  - the broker as an ECS daemon service (EC2 launch type, host networking, Docker socket mounted)
  - the internal NLB, and the broker token in Secrets Manager
- Local development: `docker compose` runs the broker. `SANDBOX_DOCKER_RUNTIME=runc` lets it
  work without gVisor installed, which is unisolated and for development only.
- Known limits:
  - the host kernel is shared, behind gVisor's Sentry
  - we own the hosts and their patching
  - the broker holds the Docker socket, which is root on the host, so it's security-critical
    code
  - tenants share hosts
  - gVisor adds overhead to I/O-heavy code
  - DNS isn't filtered (same as Fargate)

## Branch 3: Lambda, one invocation per call

- `LambdaBackend` (`aios/sandbox/aws_lambda.py`):
  - `start`: makes a session id and expiry (now + `SANDBOX_MAX_SESSION_SECONDS`), and invokes the
    function once with `{"ping": true}` to warm an environment for it. The handle is the id, the
    expiry, and the proxy URL as the secret (so `sandbox_sessions.secret` becomes `text`,
    migration 0005).
  - `alive`: whether the session is before its expiry. Nothing runs between calls, so idling
    doesn't end it.
  - `execute`: presigns a GET and a size-capped POST for the session's
    `sessions/<id>/work.tar.gz`, then invokes the function synchronously with the code, the proxy
    URL and those URLs, with `TenantId` set to the session. Never retried, so code can't run
    twice. A function error (out of memory or time) is a `SandboxError`, not `SandboxGone`:
    `/work` is as it was before the call.
  - `stop`: deletes the session's `/work`.
- The handler (`aios/sandbox/lambda_handler.py`, stdlib only, next to the runner in the
  `sandbox-lambda` image): restores `/work` unless this environment already has its latest copy
  (a conditional GET on its MD5), runs the code with the runner, and saves `/work` if it changed
  and is within the limit. `/work` is a link to `/tmp/work`, Lambda's only writable path. An
  environment that last served another session empties `/tmp` first.
- Function: the `sandbox-lambda` image, tenant isolation (`PER_TENANT`), in subnets with no
  default route, 2 GB memory, 2 GB `/tmp`, a 420 s timeout.
- Network: a security group with no ingress, and egress to the egress proxy and the S3 gateway
  endpoint only. The endpoint allows only sessions' `/work`, and the bucket refuses access from
  anywhere else. No interface endpoints: Lambda pulls the image and ships logs itself.
- IAM: the function's role has the VPC-access managed policy, which Lambda uses itself, and
  denies everything to calls carrying `lambda:SourceFunctionArn` (calls from the code). The
  API's task role can invoke the function and use the bucket's `sessions/` objects.
- Local development: none; the tests run the handler and runner for real against a fake Lambda
  and a fake S3.
- Known limits:
  - each call restores and saves `/work`, which costs time on a cold environment or a big change
  - cold starts per tenant, after a few idle minutes
  - tenant environments are capped at 2,500 per 1,000 concurrency
  - DNS isn't filtered (same as Fargate)
  - the code runs as the handler's user, so it can read its own call's presigned URLs and proxy
    token, or answer the local runtime API itself; tenant isolation keeps that to its session

## Testing

- Shared, runs in CI:
  - tool settings validation
  - the tool end to end through a run with a fake backend: session reuse, restart after the
    sandbox has gone
  - the runner for real in a subprocess: output, exit codes, timeouts, output caps, `/work`
    persistence
  - proxy token minting and verification
  - the egress proxy against local servers: auth, allowlist, private-address refusal
- Fargate: the backend against a stubbed ECS client and a mocked runner HTTP API.
- gVisor: the broker against a fake Docker CLI (checking the exact `docker run` flags), and the
  API's broker client against the broker app in-process.
- Lambda: the backend with the real handler and runner, against a fake Lambda (one simulated
  environment per tenant) and a fake S3 server taking boto3's presigned GETs and POSTs.
- Not covered by CI: real Fargate, real `runsc`, real Lambda and Terraform applies
  (`terraform validate` only).

## Out of scope (for now)

- Installing packages at run time.
- Per-user secrets passed into the sandbox.
- Files flowing between the sandbox and the rest of the app (documents, datasets).
- Scheduled, non-interactive runs (the pipeline work this plan replaces).
- DNS filtering.
- A per-execution sandbox model for untrusted, high-volume runs.

## Decision log

| Date | Decision |
| --- | --- |
| 2026-10-11 | Pivot from the data-ingestion plan to a `run_python` tool, built twice (Fargate per run; EC2 + gVisor) to compare |
| 2026-10-12 | Add a third candidate, Lambda: one invocation per call with tenant isolation per session, `/work` kept in S3 through presigned URLs |
