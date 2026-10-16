#!/usr/bin/env bash
# Does a -target apply update an output that references the target? Does a following
# -refresh-only apply (no resource changes) update it, and a newly added one, without applying
# the untargeted change?
set -euo pipefail
T=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/tfbin/terraform
D=/tmp/claude-0/-home-user-aios/16d5b276-3547-54f2-a93a-085ab108fdbf/scratchpad/tftarget
cd "$D"
rm -f terraform.tfstate terraform.tfstate.backup new.tf
"$T" init -no-color >/dev/null
"$T" apply -auto-approve -no-color >/dev/null
echo 'output "new" { value = { t = terraform_data.task.output, n = terraform_data.net.output } }' > new.tf
"$T" apply -auto-approve -no-color -var v=2 -target=terraform_data.task | grep -E "Apply complete"
echo "after -target:       task=$("$T" output -json task) other=$("$T" output -raw other) new=$("$T" output -raw new 2>&1 | head -1)"
"$T" apply -refresh-only -auto-approve -no-color -var v=2 | grep -E "Apply complete"
echo "after -refresh-only: task=$("$T" output -json task) other=$("$T" output -raw other) new=$("$T" output -raw new 2>&1 | head -1)"
echo "-- again, targeting everything the new output references:"
rm -f terraform.tfstate terraform.tfstate.backup
rm -f new.tf
"$T" apply -auto-approve -no-color >/dev/null
echo 'output "new" { value = { t = terraform_data.task.output, n = terraform_data.net.output } }' > new.tf
"$T" apply -auto-approve -no-color -var v=2 -target=terraform_data.task -target=terraform_data.net | grep -E "Apply complete"
echo "after -target (both): new=$("$T" output -json new 2>&1 | head -1) other=$("$T" output -raw other)"
echo "pending untargeted change still pending:"
"$T" plan -no-color -var v=2 | grep -E "terraform_data.other|Plan:"
rm -f new.tf
