#!/usr/bin/env bash
# Build, push and roll out a release to AWS.
#   infra/deploy.sh [image-tag]        (defaults to the current git sha)
# Needs: aws CLI (logged in), docker with buildx, terraform.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT/infra/terraform"
TAG="${1:-$(git -C "$ROOT" rev-parse --short HEAD)}"
tf() { terraform -chdir="$TF_DIR" "$@"; }

echo "==> Ensuring ECR repositories exist"
tf apply -auto-approve -input=false -target=aws_ecr_repository.this

REGION="$(tf output -raw region)"
REGISTRY="$(tf output -json ecr_repositories | python3 -c 'import json,sys; print(next(iter(json.load(sys.stdin).values())).split("/")[0])')"
repo() { tf output -json ecr_repositories | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

echo "==> Building and pushing images ($TAG)"
docker buildx build --platform linux/amd64 --target api -t "$(repo api):$TAG" --push "$ROOT/backend"
docker buildx build --platform linux/amd64 --target worker -t "$(repo worker):$TAG" --push "$ROOT/backend"
docker buildx build --platform linux/amd64 --target sandbox -t "$(repo sandbox):$TAG" --push "$ROOT/backend"
docker buildx build --platform linux/amd64 -t "$(repo web):$TAG" --push "$ROOT/frontend"

# Full apply (network, NAT, IAM, RDS, secrets, ...) so everything the migration needs
# exists, but with the services still pinned to the release that is running now:
# only the migrate task gets the new image. On a fresh stack nothing is running yet,
# so the services are created (or kept) at zero tasks until the schema exists.
CURRENT_TAG="$(tf show -json 2>/dev/null | python3 -c '
import json, sys
try:
    state = json.load(sys.stdin)
except ValueError:
    sys.exit()
resources = {
    r["address"]: r["values"]
    for r in state.get("values", {}).get("root_module", {}).get("resources", [])
}
# Only a stack whose services are actually running counts: after a first deploy that failed
# at the migration they exist at zero tasks and must stay there until a migration succeeds.
if resources.get("aws_ecs_service.this[\"api\"]", {}).get("desired_count"):
    api = resources["aws_ecs_task_definition.api"]
    print(json.loads(api["container_definitions"])[0]["image"].rsplit(":", 1)[1])
' || true)"
if [[ -n "$CURRENT_TAG" ]]; then
  echo "==> Applying infrastructure (services stay on $CURRENT_TAG; migration uses $TAG)"
  PREP_VARS=(-var "image_tag=$CURRENT_TAG")
else
  echo "==> First deploy: applying infrastructure with services scaled to zero"
  PREP_VARS=(-var "image_tag=$TAG" -var "run_services=false")
fi
tf apply -auto-approve -input=false "${PREP_VARS[@]}" -var "migrate_image_tag=$TAG"

CLUSTER="$(tf output -raw ecs_cluster)"
# Runs a one-off task (a Terraform output with its task definition and network) to completion.
run_task() {
  local output="$1" logs="$2" spec taskdef netcfg task_arn exit_code
  spec="$(tf output -json "$output")"
  taskdef="$(echo "$spec" | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_definition"])')"
  netcfg="$(echo "$spec" | python3 -c 'import json,sys; m=json.load(sys.stdin); print(json.dumps({"awsvpcConfiguration": {"subnets": m["subnets"], "securityGroups": [m["security_group"]], "assignPublicIp": "DISABLED"}}))')"
  task_arn="$(aws ecs run-task --region "$REGION" --cluster "$CLUSTER" --launch-type FARGATE \
    --task-definition "$taskdef" --network-configuration "$netcfg" \
    --query 'tasks[0].taskArn' --output text)"
  aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$task_arn"
  exit_code="$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$task_arn" \
    --query 'tasks[0].containers[0].exitCode' --output text)"
  if [[ "$exit_code" != "0" ]]; then
    echo "$output failed (exit $exit_code); see CloudWatch log group /ecs/*/$logs" >&2
    exit 1
  fi
}

# The only use of the master user: create the app's and the provisioner's roles (or update
# their passwords) and hand the app's role the schema. Services still running as the master
# user keep working: it's a member of the app's role.
echo "==> Creating database roles"
run_task db_roles_task db-roles

echo "==> Running database migrations"
run_task migrate_task migrate

echo "==> Rolling out services"
tf apply -auto-approve -input=false -var "image_tag=$TAG"
echo "Deployed $TAG -> $(tf output -raw url)"
