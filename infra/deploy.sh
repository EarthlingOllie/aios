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
docker buildx build --platform linux/amd64 -t "$(repo web):$TAG" --push "$ROOT/frontend"

echo "==> Applying infrastructure (data stores + migration task)"
tf apply -auto-approve -input=false -var "image_tag=$TAG" -target=aws_ecs_task_definition.migrate

echo "==> Running database migrations"
CLUSTER="$(tf output -raw ecs_cluster)"
MIGRATE="$(tf output -json migrate_task)"
TASKDEF="$(echo "$MIGRATE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_definition"])')"
NETCFG="$(echo "$MIGRATE" | python3 -c 'import json,sys; m=json.load(sys.stdin); print(json.dumps({"awsvpcConfiguration": {"subnets": m["subnets"], "securityGroups": [m["security_group"]], "assignPublicIp": "DISABLED"}}))')"
TASK_ARN="$(aws ecs run-task --region "$REGION" --cluster "$CLUSTER" --launch-type FARGATE \
  --task-definition "$TASKDEF" --network-configuration "$NETCFG" \
  --query 'tasks[0].taskArn' --output text)"
aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN"
EXIT_CODE="$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)"
if [[ "$EXIT_CODE" != "0" ]]; then
  echo "Migration failed (exit $EXIT_CODE); see CloudWatch log group /ecs/*/migrate" >&2
  exit 1
fi

echo "==> Rolling out services"
tf apply -auto-approve -input=false -var "image_tag=$TAG"
echo "Deployed $TAG -> $(tf output -raw url)"
